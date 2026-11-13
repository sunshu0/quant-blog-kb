"""discover：拉 sitemap → 按站点配置过滤 → 写进状态库（design.md 4.1、4.7）。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from qbkb import store
from qbkb.config import SiteConfig, ensure_allowed
from qbkb.discover.sitemap import MAX_SITEMAP_BYTES, SitemapEntry, SitemapError, parse_sitemap
from qbkb.fetch.settings import (
    DOWNLOAD_DELAY_SECONDS,
    TIMEOUT_SECONDS,
    USER_AGENT,
    site_crawl_delay,
)
from qbkb.models import DiscoveredUrl

# 站点配置里没写时的默认值（新配置项，见 PR 说明）
DEFAULT_MAX_SITEMAPS = 50
MAX_REDIRECTS = 5
# slug = 可读部分（最多 80 字符）+ "-" + 路径的 8 位 hash
SLUG_READABLE_MAX = 80
SLUG_HASH_LEN = 8

type Fetch = Callable[[str], bytes]
type Sleep = Callable[[float], None]

# 只留小写字母、数字、"_"、"-"：没有 "."，Windows 保留名（con、nul.txt……）和大小写撞名都不会出现
_SLUG_UNSAFE = re.compile(r"[^a-z0-9_-]+")


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
        exclude = tuple(section.get("exclude_prefixes") or ())
        for p in (*include, *exclude):
            if not isinstance(p, str) or not p.startswith("/"):
                raise ValueError(f"站点 {site.name}: 路径前缀必须以 / 开头: {p!r}")
        return cls(
            site=site.name,
            host=urlsplit(base_url).netloc.lower(),
            sitemaps=sitemaps,
            include_prefixes=include,
            exclude_prefixes=exclude,
            max_sitemaps=int(section.get("max_sitemaps", DEFAULT_MAX_SITEMAPS)),
        )

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """拉 sitemap 时允许访问（含重定向目标）的 host：站点本身 + 入口 sitemap 所在 host。"""
        return frozenset({self.host, *(urlsplit(s).netloc.lower() for s in self.sitemaps)})


@dataclass(slots=True)
class DiscoverStats:
    new: int = 0  # 状态库里没有的 URL
    updated: int = 0  # lastmod 变了，退回 discovered 重新处理
    unchanged: int = 0  # 已有且 lastmod 没变，跳过
    filtered: int = 0  # 不符合前缀规则、外站、语言版本、重复等，没写库
    conflicts: int = 0  # slug 和本次另一个 URL 相同，没写库（见 errors）
    sitemaps_fetched: int = 0
    errors: list[str] = field(default_factory=list)  # 嵌套 sitemap 失败、slug 冲突等，不中断整体


def normalize_url(loc: str) -> str:
    """去掉片段，scheme 和 host 转小写。路径大小写保留（服务器可能区分）。"""
    parts = urlsplit(loc.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def prefix_matches(path: str, prefix: str) -> bool:
    """按路径段匹配：/a/sub 匹配 /a/sub 和 /a/sub/x，不匹配 /a/sub-x。"""
    p = prefix.rstrip("/")
    return path == p or path.startswith(p + "/")


def to_discovered(rules: DiscoverRules, entry: SitemapEntry) -> DiscoveredUrl | None:
    """按规则把一条 sitemap 记录转成 DiscoveredUrl；不该处理的返回 None。"""
    url = normalize_url(entry.loc)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.netloc != rules.host:
        return None
    path = parts.path
    if any(prefix_matches(path, p) for p in rules.exclude_prefixes):
        return None
    prefix = next((p for p in rules.include_prefixes if prefix_matches(path, p)), None)
    if prefix is None:
        return None
    slug = make_slug(path[len(prefix.rstrip("/")) :])
    if not slug:
        return None  # 前缀本身（列表页），不是文章
    return DiscoveredUrl(url=url, site=rules.site, slug=slug, lastmod=entry.lastmod)


def make_slug(rest: str) -> str:
    """前缀之后的路径（可以带 %xx 编码）→ 文件名安全、不易撞名的 slug。

    可读部分：解码后各段用 "-" 连接、转小写、非 [a-z0-9_-] 换成 "-"、截到 80 字符；
    后缀：解码后路径的 sha1 前 8 位，区分 /a/b 与 /a-b、Foo 与 foo、纯中文路径等。
    前缀本身（没有剩余路径）返回空串。
    """
    decoded = unquote(rest).strip("/")
    segments = [s for s in decoded.split("/") if s]
    if not segments:
        return ""
    readable = _SLUG_UNSAFE.sub("-", "-".join(segments).lower()).strip("-")
    readable = readable[:SLUG_READABLE_MAX].rstrip("-")
    digest = hashlib.sha1("/".join(segments).encode("utf-8")).hexdigest()[:SLUG_HASH_LEN]
    return f"{readable}-{digest}" if readable else digest


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
    by_slug: dict[str, str] = {}
    for d in sorted(unique, key=lambda x: x.url):  # 排序：冲突时总是保留同一个
        other = by_slug.setdefault(d.slug, d.url)
        if other != d.url:
            stats.conflicts += 1
            stats.errors.append(f"slug 冲突，跳过 {d.url}（与 {other} 同为 {d.slug}）")
            continue
        existed = store.get_article(conn, d.url) is not None
        changed = store.upsert_discovered(conn, d)
        if not existed:
            stats.new += 1
        elif changed:
            stats.updated += 1
        else:
            stats.unchanged += 1
    return stats


def http_get(
    url: str,
    allowed_hosts: frozenset[str],
    *,
    max_bytes: int = MAX_SITEMAP_BYTES,
    transport: httpx.BaseTransport | None = None,
    delay: float = DOWNLOAD_DELAY_SECONDS,
    sleep: Sleep = time.sleep,
) -> bytes:
    """下载一份 sitemap。

    重定向手动跟随，目标 host 必须在 allowed_hosts 里；响应体（httpx 已按 Content-Encoding
    解码后的字节）超过 max_bytes 就中止。transport 供测试注入 httpx.MockTransport。
    """
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        transport=transport,
    ) as client:
        for attempt in range(MAX_REDIRECTS + 1):
            if attempt:
                sleep(delay)
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("Location")
                    if not location:
                        raise SitemapError(f"{url}: 重定向缺少 Location")
                    target = str(resp.url.join(location))
                    parts = urlsplit(target)
                    if parts.scheme not in ("http", "https") or (
                        parts.netloc.lower() not in allowed_hosts
                    ):
                        raise SitemapError(f"{url}: 重定向到外站 {target}，不跟随")
                    url = target
                    continue
                resp.raise_for_status()
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise SitemapError(f"{url}: 响应超过 {max_bytes} 字节，已中止")
                return bytes(buf)
    raise SitemapError(f"{url}: 重定向超过 {MAX_REDIRECTS} 次")


def collect_entries(
    rules: DiscoverRules,
    fetch: Fetch,
    stats: DiscoverStats,
    *,
    sleep: Sleep = time.sleep,
    delay: float = DOWNLOAD_DELAY_SECONDS,
) -> list[SitemapEntry]:
    """从入口 sitemap 开始广度优先展开 sitemap index。

    入口 sitemap 失败直接抛错；嵌套的失败记进 stats.errors 后继续。
    只跟随本站的嵌套 sitemap，最多抓 max_sitemaps 份，防止环和失控；两次请求之间等 delay 秒。
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
        if stats.sitemaps_fetched:
            sleep(delay)
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
    site: SiteConfig,
    conn: sqlite3.Connection,
    fetch: Fetch | None = None,
    *,
    sleep: Sleep = time.sleep,
) -> DiscoverStats:
    """对一个站点跑一次 discover。fetch / sleep 可替换成离线的假实现。"""
    ensure_allowed(site)
    rules = DiscoverRules.from_site(site)
    hosts = rules.allowed_hosts
    stats = DiscoverStats()
    # 站点要求的最小间隔（crawl_delay，比如 robots.txt 的 Crawl-delay）比默认值大时用它
    delay = max(DOWNLOAD_DELAY_SECONDS, site_crawl_delay(site.raw) or 0.0)
    entries = collect_entries(
        rules,
        fetch or (lambda url: http_get(url, hosts, delay=delay, sleep=sleep)),
        stats,
        sleep=sleep,
        delay=delay,
    )
    result = apply_entries(conn, rules, entries)
    result.sitemaps_fetched = stats.sitemaps_fetched
    result.errors = stats.errors + result.errors
    return result
