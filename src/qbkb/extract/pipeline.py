"""和状态库衔接：处理 fetched 的文章，写回段落和元数据。"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.extract.core import extract_full
from qbkb.extract.quality import check_quality
from qbkb.extract.rules import ExtractRules, load_rules
from qbkb.models import Status

log = logging.getLogger(__name__)

# 抽取阶段失败时 error 的前缀；retry_failed 只捞这一类，不碰 fetch 等阶段的失败
FAIL_PREFIX = "extract: "


@dataclass(slots=True)
class RunStats:
    extracted: int = 0
    failed: int = 0
    flagged: dict[str, list[str]] = field(default_factory=dict)


def _raw_file(raw_path: str | None, root: Path) -> Path:
    if not raw_path:
        raise FileNotFoundError("raw_path 为空")
    p = Path(raw_path)
    return p if p.is_absolute() else root / p


def extract_one(
    conn: sqlite3.Connection, url: str, rules: ExtractRules, root: Path = Path(".")
) -> list[str]:
    """抽取一篇文章并写回状态库，返回质检问题。失败时抛异常，由调用方记录。

    前置状态应是 fetched，或抽取阶段失败（retry_failed）。成功后状态改为 extracted、
    error 清空：用在已经翻译 / 等人工复查的文章上，会丢掉下游的进度和复查原因。
    """
    row = store.get_article(conn, url)
    if row is None:
        raise KeyError(url)
    raw = _raw_file(row["raw_path"], root).read_bytes()
    result = extract_full(raw, url, rules)
    issues = check_quality(result, rules)
    art = result.article
    # 两步各自一个事务（store 没有提供合并的接口）。先写段落再改状态：中途崩溃时文章
    # 仍是 fetched，下游不会读到这些段落；下次 run 会整体替换段落再推进状态，自动恢复。
    store.replace_segments(conn, url, art.segments)
    store.set_status(
        conn,
        url,
        Status.EXTRACTED,
        body_hash=art.body_hash,
        title=art.meta.title or None,
        author=art.meta.author,
        published=art.meta.published,
        tags=json.dumps(list(art.meta.tags), ensure_ascii=False),
        # 质检问题单独一列（JSON 数组），不和 fetch / translate 的失败原因混在 error 里
        qc_issues=json.dumps(issues, ensure_ascii=False) if issues else None,
        error=None,
    )
    return issues


def _extract_failures(conn: sqlite3.Connection, site: str) -> list[str]:
    """抽取阶段失败、原始 HTML 还在记录里的文章。fetch 阶段的失败（没有 raw_path，
    或 error 不是 extract 写的）留给 fetch 自己重试。"""
    out: list[str] = []
    for url in store.list_by_status(conn, Status.FAILED, site):
        row = store.get_article(conn, url)
        if row is not None and row["raw_path"] and (row["error"] or "").startswith(FAIL_PREFIX):
            out.append(url)
    return out


def run(
    conn: sqlite3.Connection,
    site: SiteConfig,
    root: Path = Path("."),
    *,
    retry_failed: bool = False,
) -> RunStats:
    """处理这个站点全部 fetched 文章。单篇失败不影响其他文章，失败原因记进状态库。

    retry_failed=True 时，抽取阶段失败过的文章也重新处理（改了规则之后用）。
    不访问网络：只读 fetch 存下的原始 HTML。
    """
    rules = load_rules(site)
    stats = RunStats()
    urls = store.list_by_status(conn, Status.FETCHED, site.name)
    if retry_failed:
        urls += _extract_failures(conn, site.name)
    for url in urls:
        try:
            issues = extract_one(conn, url, rules, root)
        except Exception as e:  # 一篇坏页面不能拖垮整批
            retries = store.record_failure(conn, url, f"{FAIL_PREFIX}{type(e).__name__}: {e}")
            log.warning("抽取失败 %s（第 %d 次）：%s", url, retries, e)
            stats.failed += 1
            continue
        stats.extracted += 1
        if issues:
            stats.flagged[url] = issues
    return stats
