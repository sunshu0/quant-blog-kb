"""discover：拉 sitemap → 按站点配置过滤 → 写进状态库（design.md 4.1、4.7）。"""

from __future__ import annotations

import re
import sqlite3
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from qbkb import store
from qbkb.config import SiteConfig, ensure_allowed
from qbkb.discover.sitemap import SitemapEntry, SitemapError, parse_sitemap
from qbkb.fetch.settings import TIMEOUT_SECONDS, USER_AGENT
from qbkb.models import DiscoveredUrl

# 站点配置里没写时的默认值（新配置项，见 PR 说明）
DEFAULT_MAX_SITEMAPS = 50

type Fetch = Callable[[str], bytes]

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True, slots=True)
class DiscoverRules:
    """从站点配置的 discover 节解析出来的规则。"""

    site: str
    host: str
    sitemaps: tuple[str, ...]
    include_prefixes: tuple[str, ...]
    exclude_prefixes: tuple[str, ...] = ()
    max_sitemaps: int = DEFAULT_MAX_SITEMAPS

    @classmethod
    def from_site(cls, site: SiteConfig) -> DiscoverRules:
        raw = site.raw
        base_url = raw.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            raise ValueError(f"站点 {site.name}: 缺少 base_url")
        section: dict[str, Any] = raw.get("discover") or {}
        sitemap = section.get("sitemap") or base_url.rstrip("/") + "/sitemap.xml"
        sitemaps = (sitemap,) if isinstance(sitemap, str) else tuple(sitemap)
        include = tuple(section.get("include_prefixes") or ("/",))
        return cls(
            site=site.name,
            host=urlsplit(base_url).netloc.lower(),
            sitemaps=sitemaps,
            include_prefixes=include,
            exclude_prefixes=tuple(section.get("exclude_prefixes") or ()),
            max_sitemaps=int(section.get("max_sitemaps", DEFAULT_MAX_SITEMAPS)),
        )


@dataclass(slots=True)
class DiscoverStats:
    new: int = 0  # 状态库里没有的 URL
    updated: int = 0  # lastmod 变了，退回 discovered 重新处理
    unchanged: int = 0  # 已有且 lastmod 没变，跳过
    filtered: int = 0  # 不符合前缀规则、外站、语言版本等，没写库
    sitemaps_fetched: int = 0
    errors: list[str] = field(default_factory=list)  # 嵌套 sitemap 的失败，不中断整体


def normalize_url(loc: str) -> str:
    """去掉片段，scheme 和 host 转小写。路径大小写保留（服务器可能区分）。"""
    parts = urlsplit(loc.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _matches(path: str, prefix: str) -> bool:
    return path.startswith(prefix)


def to_discovered(rules: DiscoverRules, entry: SitemapEntry) -> DiscoveredUrl | None:
    """按规则把一条 sitemap 记录转成 DiscoveredUrl；不该处理的返回 None。"""
    url = normalize_url(entry.loc)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc != rules.host:
        return None
    path = parts.path
    if any(_matches(path, p) for p in rules.exclude_prefixes):
        return None
    prefix = next((p for p in rules.include_prefixes if _matches(path, p)), None)
    if prefix is None:
        return None
    slug = make_slug(path[len(prefix) :])
    if not slug:
        return None  # 前缀本身（列表页），不是文章
    return DiscoveredUrl(url=url, site=rules.site, slug=slug, lastmod=entry.lastmod)


def make_slug(rest: str) -> str:
    """前缀之后的路径 → 文件名安全的 slug。多级路径用 "-" 连接，避免不同文章撞名。"""
    segments = [s for s in rest.split("/") if s]
    slug = "-".join(_SLUG_UNSAFE.sub("-", s).strip("-") for s in segments)
    return slug.lstrip(".-")


def _dedupe(items: Iterable[DiscoveredUrl]) -> list[DiscoveredUrl]:
    """同一 URL 出现多次时保留 lastmod 最新的一条。"""
    best: dict[str, DiscoveredUrl] = {}
    for d in items:
        old = best.get(d.url)
        if old is None or (d.lastmod or "") > (old.lastmod or ""):
            best[d.url] = d
    return list(best.values())


def apply_entries(
    conn: sqlite3.Connection, rules: DiscoverRules, entries: Iterable[SitemapEntry]
) -> DiscoverStats:
    """过滤 sitemap 记录并写进状态库。不访问网络。"""
    stats = DiscoverStats()
    candidates: list[DiscoveredUrl] = []
    for entry in entries:
        d = to_discovered(rules, entry)
        if d is None:
            stats.filtered += 1
        else:
            candidates.append(d)
    unique = _dedupe(candidates)
    stats.filtered += len(candidates) - len(unique)
    for d in unique:
        existed = store.get_article(conn, d.url) is not None
        changed = store.upsert_discovered(conn, d)
        if not existed:
            stats.new += 1
        elif changed:
            stats.updated += 1
        else:
            stats.unchanged += 1
    return stats


def http_get(url: str) -> bytes:
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS, follow_redirects=True
    ) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def collect_entries(rules: DiscoverRules, fetch: Fetch, stats: DiscoverStats) -> list[SitemapEntry]:
    """从入口 sitemap 开始广度优先展开 sitemap index。

    入口 sitemap 失败直接抛错；嵌套的失败记进 stats.errors 后继续。
    只跟随本站的嵌套 sitemap，最多抓 max_sitemaps 份，防止环和失控。
    """
    queue = deque(rules.sitemaps)
    seen = set(queue)
    roots = set(rules.sitemaps)
    entries: list[SitemapEntry] = []
    while queue:
        url = queue.popleft()
        if stats.sitemaps_fetched >= rules.max_sitemaps:
            stats.errors.append(f"已达 max_sitemaps={rules.max_sitemaps}，未抓取: {url}")
            continue
        try:
            stats.sitemaps_fetched += 1
            parsed = parse_sitemap(fetch(url))
        except (httpx.HTTPError, SitemapError) as e:
            if url in roots:
                raise
            stats.errors.append(f"{url}: {e}")
            continue
        entries.extend(parsed.urls)
        for child in parsed.sitemaps:
            loc = normalize_url(child.loc)
            if urlsplit(loc).netloc == rules.host and loc not in seen:
                seen.add(loc)
                queue.append(loc)
    return entries


def discover_site(
    site: SiteConfig, conn: sqlite3.Connection, fetch: Fetch | None = None
) -> DiscoverStats:
    """对一个站点跑一次 discover。fetch 可替换成离线的假实现。"""
    ensure_allowed(site)
    rules = DiscoverRules.from_site(site)
    stats = DiscoverStats()
    entries = collect_entries(rules, fetch or http_get, stats)
    result = apply_entries(conn, rules, entries)
    result.sitemaps_fetched = stats.sitemaps_fetched
    result.errors = stats.errors
    return result
