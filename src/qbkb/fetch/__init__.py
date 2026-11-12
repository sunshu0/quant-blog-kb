"""抓取（Scrapy，限速、重试、存原始 HTML、条件请求）。Phase 1 A 线负责。

本模块和 settings、outcome 都不导入 Scrapy；Scrapy 只在 run_fetch 里、以及
`qbkb.fetch.process` / `qbkb.fetch.spider` 里加载。需要响应处理函数时直接从 process 导入。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from qbkb import store
from qbkb.config import SiteConfig, ensure_allowed
from qbkb.extract.rules import ExtractRules
from qbkb.fetch.outcome import Kind, Outcome
from qbkb.fetch.settings import USER_AGENT, build_settings
from qbkb.models import Status
from qbkb.pdf import load_pdf_rules

__all__ = [
    "FetchStats",
    "Kind",
    "Outcome",
    "USER_AGENT",
    "build_settings",
    "run_fetch",
    "select_pdf_targets",
    "select_targets",
]


@dataclass(frozen=True, slots=True)
class FetchStats:
    fetched: int = 0
    not_modified: int = 0
    pdf: int = 0
    failed: int = 0
    stale_tmp_removed: int = 0
    # 整站停止的原因（robots 跨站、Retry-After 太久……）；没有停止就是 None
    stop_reason: str | None = None
    pdf_downloaded: int = 0
    pdf_failed: int = 0

    @classmethod
    def from_counts(
        cls,
        counts: Counter[Kind],
        stale_tmp_removed: int = 0,
        stop_reason: str | None = None,
        pdf_counts: Counter[str] | None = None,
    ) -> FetchStats:
        pdf_counts = pdf_counts or Counter()
        return cls(
            fetched=counts[Kind.FETCHED],
            not_modified=counts[Kind.NOT_MODIFIED],
            pdf=counts[Kind.PDF],
            failed=counts[Kind.FAILED],
            stale_tmp_removed=stale_tmp_removed,
            stop_reason=stop_reason,
            pdf_downloaded=pdf_counts["ok"],
            pdf_failed=pdf_counts["failed"],
        )


def select_targets(
    db_path: str | Path, site: str, urls: Sequence[str] | None = None, limit: int | None = None
) -> list[str]:
    """默认取该站点全部 discovered；给了 urls（如每月复查）就只取其中属于该站点的。"""
    conn = store.connect(db_path)
    try:
        if urls is None:
            targets = store.list_by_status(conn, Status.DISCOVERED, site)
        else:
            targets = []
            for u in urls:
                row = store.get_article(conn, u)
                if row is not None and row["site"] == site:
                    targets.append(u)
    finally:
        conn.close()
    return targets if limit is None else targets[:limit]


def select_pdf_targets(
    conn: sqlite3.Connection,
    site: str,
    urls: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[str]:
    """需要（重新）下载站内 PDF 正文的文章：HTML 已抓、但 PDF 还没下载或文件丢了。

    只挑 fetched、或失败原因以 ``pdf:`` 开头的 failed 文章（后者是 PDF 下载失败、可续跑）；
    不碰 HTML 抓取失败（403/人机验证等）和已翻译 / 已发布的文章。给了 urls 就只考虑其中
    属于该站点的文章，limit 与主 targets 共享预算。
    """
    wanted = set(urls) if urls is not None else None
    out: list[str] = []
    for row in store.list_rows(conn, (Status.FETCHED, Status.FAILED), site):
        if not row["raw_path"]:
            continue
        if row["status"] == Status.FAILED.value and not (row["error"] or "").startswith("pdf:"):
            continue  # HTML 抓取失败，不是 PDF 待续，不能借补 PDF 掩盖
        url = str(row["url"])
        if wanted is not None and url not in wanted:
            continue
        pdf_path = row["pdf_path"]
        if pdf_path and Path(pdf_path).is_file():
            continue  # 已下载，不重下
        out.append(url)
    return out if limit is None else out[:limit]


def run_fetch(
    site: SiteConfig,
    db_path: str | Path,
    raw_dir: str | Path = "raw",
    *,
    urls: Sequence[str] | None = None,
    limit: int | None = None,
    http_cache: bool = False,
    log_level: str = "INFO",
    settings_overrides: dict[str, object] | None = None,
) -> FetchStats:
    """抓取并落库。会启动 Twisted reactor，所以一个进程只能调用一次（CLI 场景足够）。"""
    ensure_allowed(site)
    base_url = site.raw.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError(f"站点 {site.name}: 缺少 base_url")
    targets = select_targets(db_path, site.name, urls, limit)
    pdf_rules = load_pdf_rules(site)  # 配置写错在这里尽早报错；没配置返回 None
    extract_rules: ExtractRules | None = None
    if pdf_rules is not None:
        from qbkb.extract.rules import load_rules

        extract_rules = load_rules(site)  # 补 PDF 前要用抽取口径判断正文够不够长
    if not targets and pdf_rules is None:
        return FetchStats()
    # 延迟导入：discover、状态统计等只用常量的地方不必加载 Scrapy
    from scrapy.crawler import CrawlerProcess

    from qbkb.fetch.process import cleanup_stale_tmp
    from qbkb.fetch.spider import ArticleSpider

    raw = Path(raw_dir)
    removed = cleanup_stale_tmp(raw, site.name)
    counts: Counter[Kind] = Counter()
    pdf_counts: Counter[str] = Counter()
    conn = store.connect(db_path)
    stop_reason: str | None = None
    try:
        if pdf_rules is not None:
            pdf_urls = select_pdf_targets(conn, site.name, urls=urls, limit=limit)
            # 已在主 targets 里的文章由 parse_article 重新调度 PDF，避免同一份文件被请求两次
            target_set = set(targets)
            pdf_urls = [u for u in pdf_urls if u not in target_set]
        else:
            pdf_urls = []
        if not targets and not pdf_urls:
            return FetchStats()
        settings = build_settings(
            http_cache=http_cache, log_level=log_level, **(settings_overrides or {})
        )
        process = CrawlerProcess(settings=settings)
        # 显式 create_crawler 是为了在 process.start() 之后还能拿到 spider，读回 stop_reason
        crawler = process.create_crawler(ArticleSpider)
        process.crawl(
            crawler,
            conn=conn,
            urls=targets,
            raw_dir=raw,
            allowed_host=urlsplit(base_url).hostname or "",
            allowed_scheme=urlsplit(base_url).scheme or "https",
            counts=counts,
            pdf_rules=pdf_rules,
            pdf_urls=pdf_urls,
            pdf_counts=pdf_counts,
            extract_rules=extract_rules,
        )
        process.start()
        stop_reason = getattr(crawler.spider, "stop_reason", None)
    finally:
        conn.close()
    return FetchStats.from_counts(counts, removed, stop_reason, pdf_counts)
