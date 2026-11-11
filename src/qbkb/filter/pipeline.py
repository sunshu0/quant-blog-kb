"""filter 阶段：L3 人工覆盖 → L1 规则 → L2 打分（可复用）→ filtered / skipped。"""

from __future__ import annotations

import html
import logging
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from qbkb import store
from qbkb.filter.rules import Decision, FilterConfig, Override, SiteRules
from qbkb.filter.scorers import ScoreFormatError, ScorerUnavailableError
from qbkb.models import QuotaExceededError, Score, Scorer, Segment, Status

log = logging.getLogger(__name__)

# 打分失败时 error 的前缀（文章留在 extracted，下次 filter 再试）
FAIL_PREFIX = "filter: "
# 人工覆盖只作用于还没进入翻译的文章
OVERRIDABLE = (Status.EXTRACTED, Status.FILTERED, Status.SKIPPED)

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(slots=True)
class FilterStats:
    kept: int = 0  # → filtered
    skipped_l1: int = 0
    skipped_l2: int = 0
    overridden: int = 0  # 按 overrides.yaml 处理
    scored: int = 0  # 这次真正调用了打分后端
    reused: int = 0  # 正文和评分标准都没变，复用旧打分
    errors: list[str] = field(default_factory=list)  # 打分输出不合格等，文章留在 extracted


class FilterStopped(Exception):
    """filter 中途停下。已经处理的文章都已落库，下次运行接着处理剩下的。"""

    def __init__(self, stats: FilterStats, reason: str) -> None:
        super().__init__(reason)
        self.stats = stats


class FilterQuotaStopped(FilterStopped, QuotaExceededError):
    """打分额度用完。"""


class FilterBackendStopped(FilterStopped):
    """打分后端暂时不可用。"""


def _text(html_fragment: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", html_fragment))).strip()


def body_segments(segments: list[Segment], n_body: int | None) -> list[Segment]:
    """正文部分（去掉文末的版权声明等附录）。老数据没有 body_segments 就当全是正文。"""
    return segments if n_body is None else segments[:n_body]


def body_text(conn: sqlite3.Connection, url: str, n_body: int | None) -> str:
    segs = body_segments(store.get_segments(conn, url), n_body)
    return "\n".join(t for t in (_text(s.html) for s in segs) if t)


def word_count(text: str) -> int:
    return len(text.split())


def excerpt(text: str, words: int) -> str:
    """正文前 words 个单词（L2 只看这么多）。"""
    return " ".join(text.split()[:words])


def _prefix_matches(path: str, prefix: str) -> bool:
    p = prefix.rstrip("/")
    return path == p or path.startswith(p + "/")


def check_l1(url: str, words: int, rules: SiteRules) -> str | None:
    """L1 规则。不通过时返回原因。"""
    path = urlsplit(url).path
    for prefix in rules.exclude_prefixes:
        if _prefix_matches(path, prefix):
            return f"路径在排除栏目 {prefix} 下"
    if words < rules.min_words:
        return f"正文只有 {words} 个单词，少于 {rules.min_words}"
    return None


def _stored_score(row: sqlite3.Row) -> Score | None:
    if row["value_score"] is None or row["category"] is None or row["rubric_version"] is None:
        return None
    return Score(
        value=float(row["value_score"]),
        category=str(row["category"]),
        evergreen=bool(row["evergreen"]),
        reason=str(row["score_reason"] or ""),
        rubric_version=str(row["rubric_version"]),
    )


def reusable_score(row: sqlite3.Row, version: str) -> Score | None:
    """正文没变（body_hash == scored_body_hash）且打分版本相同，就复用旧打分。"""
    if row["body_hash"] is None or row["scored_body_hash"] != row["body_hash"]:
        return None
    score = _stored_score(row)
    return score if score is not None and score.rubric_version == version else None


def apply_override(conn: sqlite3.Connection, url: str, override: Override) -> Status:
    """L3：按人工判断设状态。不动打分列（去掉覆盖后，旧打分还能复用）。"""
    row = store.get_article(conn, url)
    if row is None:
        raise KeyError(url)
    status = Status.FILTERED if override.decision is Decision.INCLUDE else Status.SKIPPED
    note = f"：{override.note}" if override.note else ""
    fields: dict[str, str | None] = {
        "score_reason": f"人工覆盖（L3，{override.decision.value}）{note}",
        "error": None,
    }
    if override.category:
        fields["category"] = override.category
    store.set_status(conn, url, status, **fields)
    return status


def filter_article(
    conn: sqlite3.Connection,
    url: str,
    cfg: FilterConfig,
    scorer: Scorer,
    overrides: Mapping[str, Override],
    stats: FilterStats,
) -> Status:
    """筛选一篇文章，返回新状态。额度用完 / 后端不可用时异常往外抛，文章状态不变。"""
    row = store.get_article(conn, url)
    if row is None:
        raise KeyError(url)
    override = overrides.get(url)
    if override is not None:
        stats.overridden += 1
        return apply_override(conn, url, override)

    text = body_text(conn, url, row["body_segments"])
    reason = check_l1(url, word_count(text), cfg.site(str(row["site"])))
    if reason is not None:
        stats.skipped_l1 += 1
        store.set_status(conn, url, Status.SKIPPED, score_reason=f"L1：{reason}", error=None)
        return Status.SKIPPED

    score = reusable_score(row, scorer.version)
    if score is not None:
        stats.reused += 1
    else:
        score = scorer.score(str(row["title"] or ""), excerpt(text, cfg.excerpt_words))
        stats.scored += 1
        if score.category not in cfg.categories:
            raise ScoreFormatError(f"类别 {score.category!r} 不在 filters.yaml 的列表里")
    keep = cfg.keeps(score.value, score.category)
    status = Status.FILTERED if keep else Status.SKIPPED
    if keep:
        stats.kept += 1
    else:
        stats.skipped_l2 += 1
    store.set_status(
        conn,
        url,
        status,
        value_score=score.value,
        category=score.category,
        evergreen=int(score.evergreen),
        score_reason=score.reason,
        rubric_version=scorer.version,
        scored_body_hash=row["body_hash"],
        error=None,
    )
    return status


def targets(
    conn: sqlite3.Connection,
    site: str,
    overrides: Mapping[str, Override],
    *,
    refilter: bool = False,
    limit: int | None = None,
) -> list[str]:
    """要处理的文章：全部 extracted；refilter 时加上 filtered / skipped（改了阈值或规则之后）；
    另外，人工覆盖里点名的 filtered / skipped 文章每次都重新套用（改了 category / note 也生效）。"""
    urls = store.list_by_status(conn, Status.EXTRACTED, site)
    if refilter:
        urls += store.list_by_status(conn, Status.FILTERED, site)
        urls += store.list_by_status(conn, Status.SKIPPED, site)
    for url in overrides:
        row = store.get_article(conn, url)
        if row is not None and row["site"] == site and row["status"] in OVERRIDABLE:
            urls.append(url)
    urls = sorted(set(urls))
    return urls if limit is None else urls[:limit]


def run(
    conn: sqlite3.Connection,
    site: str,
    cfg: FilterConfig,
    scorer: Scorer,
    overrides: Mapping[str, Override] | None = None,
    *,
    refilter: bool = False,
    limit: int | None = None,
) -> FilterStats:
    """筛选这个站点待处理的文章。

    额度用完抛 FilterQuotaStopped、后端不可用抛 FilterBackendStopped（带 stats）；
    单篇打分输出不合格只记进 stats.errors 和 error 列，文章留在 extracted，下次再试。
    """
    overrides = overrides or {}
    stats = FilterStats()
    for url in targets(conn, site, overrides, refilter=refilter, limit=limit):
        try:
            filter_article(conn, url, cfg, scorer, overrides, stats)
        except QuotaExceededError as e:
            raise FilterQuotaStopped(stats, f"打分额度用完（{e}）") from e
        except ScorerUnavailableError as e:
            raise FilterBackendStopped(stats, str(e)) from e
        except ScoreFormatError as e:
            msg = f"{FAIL_PREFIX}打分输出不合格：{e}"
            stats.errors.append(f"{url}：{msg}")
            log.warning("%s %s", url, msg)
            row = store.get_article(conn, url)
            if row is not None and row["status"] == Status.EXTRACTED:
                store.set_status(conn, url, Status.EXTRACTED, error=msg)
    return stats
