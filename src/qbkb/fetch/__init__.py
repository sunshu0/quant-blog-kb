"""抓取（Scrapy，限速、重试、存原始 HTML、条件请求）。Phase 1 A 线负责。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from qbkb import store
from qbkb.config import SiteConfig, ensure_allowed
from qbkb.fetch.process import (
    Kind,
    Outcome,
    apply_outcome,
    classify_error,
    classify_response,
    conditional_headers,
    detect_challenge,
    process_error,
    process_response,
    raw_file,
)
from qbkb.fetch.settings import USER_AGENT, build_settings
from qbkb.models import Status

__all__ = [
    "FetchStats",
    "Kind",
    "Outcome",
    "USER_AGENT",
    "apply_outcome",
    "build_settings",
    "classify_error",
    "classify_response",
    "conditional_headers",
    "detect_challenge",
    "process_error",
    "process_response",
    "raw_file",
    "run_fetch",
    "select_targets",
]


@dataclass(frozen=True, slots=True)
class FetchStats:
    fetched: int = 0
    not_modified: int = 0
    pdf: int = 0
    failed: int = 0

    @classmethod
    def from_counts(cls, counts: Counter[Kind]) -> FetchStats:
        return cls(
            fetched=counts[Kind.FETCHED],
            not_modified=counts[Kind.NOT_MODIFIED],
            pdf=counts[Kind.PDF],
            failed=counts[Kind.FAILED],
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


def run_fetch(
    site: SiteConfig,
    db_path: str | Path,
    raw_dir: str | Path = "raw",
    *,
    urls: Sequence[str] | None = None,
    limit: int | None = None,
    http_cache: bool = False,
    log_level: str = "INFO",
) -> FetchStats:
    """抓取并落库。会启动 Twisted reactor，所以一个进程只能调用一次（CLI 场景足够）。"""
    ensure_allowed(site)
    # 延迟导入：只做离线处理（测试、extract 读结果）时不必加载整个 Scrapy 爬虫栈
    from scrapy.crawler import CrawlerProcess

    from qbkb.fetch.spider import ArticleSpider

    base_url = site.raw.get("base_url")
    if not isinstance(base_url, str):
        raise ValueError(f"站点 {site.name}: 缺少 base_url")
    targets = select_targets(db_path, site.name, urls, limit)
    counts: Counter[Kind] = Counter()
    if not targets:
        return FetchStats()
    conn = store.connect(db_path)
    try:
        process = CrawlerProcess(
            settings=build_settings(http_cache=http_cache, log_level=log_level)
        )
        process.crawl(
            ArticleSpider,
            conn=conn,
            urls=targets,
            raw_dir=Path(raw_dir),
            allowed_host=urlsplit(base_url).hostname or "",
            counts=counts,
        )
        process.start()
    finally:
        conn.close()
    return FetchStats.from_counts(counts)
