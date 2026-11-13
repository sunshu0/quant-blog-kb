"""和状态库衔接：处理 fetched 的文章，写回段落、元数据和栏目。

v2（design.md 4.9）：抽取时顺带判定栏目（section_key / section），质量问题（正文太短、
抽取异常、栏目核对不一致……）只写进 qc_issues，文章照常进入 extracted；
最后按栏目配置把不采集的文章标成 unselected。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from lxml.html import document_fromstring

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.extract.clean import block_text
from qbkb.extract.core import decode_html, extract_full
from qbkb.extract.pdf import extract_pdf_text
from qbkb.extract.quality import check_quality
from qbkb.extract.rules import ExtractRules, load_rules
from qbkb.models import Segment, Status, body_hash
from qbkb.pdf import PdfRules, load_pdf_rules
from qbkb.sections import (
    ApplyStats,
    NewSection,
    SectionRules,
    apply_sections,
    load_section_rules,
    new_sections,
    page_section,
)

log = logging.getLogger(__name__)

# 抽取阶段失败时 error 的前缀；retry_failed 只捞这一类，不碰 fetch 等阶段的失败
FAIL_PREFIX = "extract: "
# v1 留下的、还没按 v2 规则判定栏目的文章：重新抽取（只读本地原始 HTML，不联网、不花额度）
LEGACY_REENTER = (Status.EXTRACTED, Status.FILTERED, Status.SKIPPED)
# 这些状态的老数据只补记栏目，不改状态（翻译、发布的进度不能丢）
LEGACY_BACKFILL = (
    Status.TRANSLATED,
    Status.NEEDS_REVIEW,
    Status.PUBLISHED,
    Status.UNSELECTED,
)


@dataclass(slots=True)
class RunStats:
    extracted: int = 0
    failed: int = 0
    flagged: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    legacy: int = 0  # 其中 v1 留下、重新进入流程的
    backfilled: int = 0  # 已翻译 / 已发布的老数据补记了栏目
    sections: ApplyStats = field(default_factory=ApplyStats)
    new: list[NewSection] = field(default_factory=list[NewSection])


def _raw_file(raw_path: str | None, root: Path) -> Path:
    if not raw_path:
        raise FileNotFoundError("raw_path 为空")
    p = Path(raw_path)
    return p if p.is_absolute() else root / p


def _reindex(segments: Sequence[Segment]) -> tuple[Segment, ...]:
    return tuple(Segment(idx=i, html=s.html) for i, s in enumerate(segments))


def _paragraph_html(text: str) -> str:
    """PDF 段落是纯文本，包成 `<p>` 才能进 segments 表、和 HTML 正文走同一套下游。"""
    return f"<p>{escape(text)}</p>"


def extract_one(
    conn: sqlite3.Connection,
    url: str,
    rules: ExtractRules,
    root: Path = Path("."),
    sections: SectionRules | None = None,
    pdf_rules: PdfRules | None = None,
) -> list[str]:
    """抽取一篇文章并写回状态库，返回质检问题。失败时抛异常，由调用方记录。

    前置状态应是 fetched，或抽取阶段失败（retry_failed），或 v1 留下的老数据。成功后状态改为
    extracted、error 清空：用在已经翻译 / 等人工复查的文章上，会丢掉下游的进度和复查原因。
    质检问题只记录，不挡住文章。

    配置了 `pdf:` 节的站点：网页正文（抽取后）短于 pdf.min_chars 且已下载了站内 PDF 时，
    把 PDF 正文段落接在网页摘要后面一起入库（摘要保留为正文开头）；公式 / 表格按原文文字提取，
    记一条质检提示。没配置 PDF 的站点行为完全不变。
    """
    row = store.get_article(conn, url)
    if row is None:
        raise KeyError(url)
    raw = _raw_file(row["raw_path"], root).read_bytes()
    result = extract_full(raw, url, rules)
    sec_rules = sections or SectionRules(site=rules.site)
    raw_text = decode_html(raw)
    page = page_section(document_fromstring(raw_text), raw_text, url, sec_rules)
    section = sec_rules.classify(url, page.key)
    art = result.article

    # 网页摘要保留为正文开头；需要时把 PDF 正文段落接在它后面。
    summary = art.segments[: result.n_body]
    keep = art.segments[result.n_body :]
    body_chars = sum(len(block_text(s.html)) for s in summary)
    pdf_segments: tuple[Segment, ...] = ()
    pdf_chars = 0
    if pdf_rules is not None and row["pdf_path"] and body_chars < pdf_rules.min_chars:
        pdf_bytes = _raw_file(row["pdf_path"], root).read_bytes()
        paragraphs = extract_pdf_text(pdf_bytes, noise_patterns=pdf_rules.noise_patterns)
        pdf_segments = tuple(Segment(idx=0, html=_paragraph_html(p)) for p in paragraphs)
        pdf_chars = sum(len(p) for p in paragraphs)
    # 正文太短按接上 PDF 后的总正文判断：短摘要 + PDF 全文不再误报
    issues = check_quality(result, rules, total_chars=body_chars + pdf_chars)
    if pdf_segments:
        issues.append("PDF 正文：公式、表格按原文文字提取，可能不完整")
    issues += page.issues
    segments = _reindex((*summary, *pdf_segments, *keep))
    n_body = len(summary) + len(pdf_segments)

    # 两步各自一个事务（store 没有提供合并的接口）。先写段落再改状态：中途崩溃时文章
    # 仍是 fetched，下游不会读到这些段落；下次 run 会整体替换段落再推进状态，自动恢复。
    store.replace_segments(conn, url, segments)
    store.set_status(
        conn,
        url,
        Status.EXTRACTED,
        body_hash=body_hash(segments),
        body_segments=n_body,
        title=art.meta.title or None,
        author=art.meta.author,
        published=art.meta.published,
        tags=json.dumps(list(art.meta.tags), ensure_ascii=False),
        # 质检问题单独一列（JSON 数组），不和 fetch / translate 的失败原因混在 error 里
        qc_issues=json.dumps(issues, ensure_ascii=False) if issues else None,
        section_key=page.key,
        section=None if section is None else section.id,
        error=None,
    )
    return issues


def backfill_section(
    conn: sqlite3.Connection, url: str, sections: SectionRules, root: Path = Path(".")
) -> bool:
    """只补记栏目（读原始 HTML），不改状态、不重新抽取。原始 HTML 不在就返回 False。"""
    row = store.get_article(conn, url)
    if row is None or not row["raw_path"]:
        return False
    path = _raw_file(row["raw_path"], root)
    if not path.is_file():
        return False
    raw_text = decode_html(path.read_bytes())
    page = page_section(document_fromstring(raw_text), raw_text, url, sections)
    section = sections.classify(url, page.key)
    store.set_fields(
        conn, url, section_key=page.key, section=None if section is None else section.id
    )
    return True


def _extract_failures(conn: sqlite3.Connection, site: str) -> list[str]:
    """抽取阶段失败、原始 HTML 还在记录里的文章。fetch 阶段的失败（没有 raw_path，
    或 error 不是 extract 写的）留给 fetch 自己重试。"""
    out: list[str] = []
    for url in store.list_by_status(conn, Status.FAILED, site):
        row = store.get_article(conn, url)
        if row is not None and row["raw_path"] and (row["error"] or "").startswith(FAIL_PREFIX):
            out.append(url)
    return out


def _legacy(
    conn: sqlite3.Connection, site: str, statuses: tuple[Status, ...], root: Path
) -> list[str]:
    """还没按 v2 规则判定栏目（section_key 为 NULL）、原始 HTML 还在的文章。"""
    out: list[str] = []
    for row in store.list_rows(conn, statuses, site):
        if row["section_key"] is not None or not row["raw_path"]:
            continue
        if _raw_file(row["raw_path"], root).is_file():
            out.append(str(row["url"]))
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
    v1 留下的 extracted / filtered / skipped 老数据也重新抽取一次，按 v2 规则进入流程
    （见 design.md 4.9 的迁移策略）。不访问网络：只读 fetch 存下的原始 HTML。
    """
    rules = load_rules(site)
    sec_rules = load_section_rules(site)
    pdf_rules = load_pdf_rules(site)
    stats = RunStats()
    urls = store.list_by_status(conn, Status.FETCHED, site.name)
    if retry_failed:
        urls += _extract_failures(conn, site.name)
    legacy = _legacy(conn, site.name, LEGACY_REENTER, root)
    for url in urls + legacy:
        try:
            issues = extract_one(conn, url, rules, root, sec_rules, pdf_rules)
        except Exception as e:  # 一篇坏页面不能拖垮整批
            retries = store.record_failure(conn, url, f"{FAIL_PREFIX}{type(e).__name__}: {e}")
            log.warning("抽取失败 %s（第 %d 次）：%s", url, retries, e)
            stats.failed += 1
            continue
        stats.extracted += 1
        if url in legacy:
            stats.legacy += 1
        if issues:
            stats.flagged[url] = issues
    for url in _legacy(conn, site.name, LEGACY_BACKFILL, root):
        try:
            if backfill_section(conn, url, sec_rules, root):
                stats.backfilled += 1
        except Exception as e:  # 补记栏目只是锦上添花，出错不影响流程
            log.warning("补记栏目失败 %s：%s", url, e)
    stats.sections = apply_sections(conn, site, sec_rules)
    stats.new = new_sections(conn, site, sec_rules)
    return stats
