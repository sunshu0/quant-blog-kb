"""离线表格快照：一篇一行，不请求网站、翻译或 LLM。"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.export.markdown import tidy_zh_spacing, to_markdown
from qbkb.export.pipeline import firm_name, write_if_changed
from qbkb.models import Status
from qbkb.sections import load_section_rules
from qbkb.summarize import valid_summary
from qbkb.translate import Glossary, lookup_translation, translated_title

COLUMNS: tuple[str, ...] = (
    "英文标题",
    "中文标题",
    "网址",
    "英文正文",
    "中文正文",
    "发布时间",
    "中文摘要",
    "作者",
    "标签",
    "公司名",
    "栏目",
    "抓取时间",
    "质量提示",
    "翻译状态",
)
STATES = (
    Status.EXTRACTED,
    Status.FILTERED,
    Status.TRANSLATED,
    Status.PUBLISHED,
    Status.NEEDS_REVIEW,
)


@dataclass(frozen=True, slots=True)
class TableStats:
    rows: int
    csv: Path
    jsonl: Path


def _array(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return [value]
    return [str(v) for v in parsed] if isinstance(parsed, list) else [str(parsed)]


def records(conn: sqlite3.Connection, site: SiteConfig, glossary: Glossary) -> list[dict[str, Any]]:
    """保留未翻译记录；只有完整有效译文才输出中文正文，不把 rejected 译文当成可用译文。"""
    rules = load_section_rules(site)
    result: list[dict[str, Any]] = []
    for row in sorted(store.list_rows(conn, STATES, site.name), key=lambda r: str(r["url"])):
        # 已花费翻译额度的下游历史记录与 Markdown 导出一致，不因取消栏目而消失。
        if row["status"] in (Status.EXTRACTED, Status.FILTERED):
            if rules.enabled and row["section_key"] is None:
                continue
            if not rules.collects(str(row["url"]), row["section_key"]):
                continue
        segments = store.get_segments(conn, row["url"])
        if not segments:
            continue
        backend = row["translator"]
        translations = [
            lookup_translation(conn, s.html, str(backend), glossary) if backend else None
            for s in segments
        ]
        complete = bool(translations) and all(t is not None for t in translations)
        en = "\n\n".join(to_markdown(s.html) for s in segments)
        zh = (
            "\n\n".join(tidy_zh_spacing(to_markdown(t or "")) for t in translations)
            if complete
            else ""
        )
        title_zh = row["title_zh"] or (
            translated_title(conn, row["title"], str(backend), glossary) if backend else None
        )
        section = rules.name_of(row["section"]) or row["section"] or ""
        summary = valid_summary(conn, row)
        issues = _array(row["qc_issues"])
        if summary and row["summary_truncated"]:
            issues.append("摘要仅基于截取的正文")
        result.append(
            dict(
                zip(
                    COLUMNS,
                    (
                        row["title"] or "",
                        title_zh or "",
                        row["url"],
                        en,
                        zh,
                        row["published"] or "",
                        summary,
                        row["author"] or "",
                        _array(row["tags"]),
                        firm_name(site),
                        section,
                        row["fetched_at"] or "",
                        issues,
                        "incomplete" if backend and not complete else str(row["status"]),
                    ),
                    strict=True,
                )
            )
        )
    return result


def write_tables(
    conn: sqlite3.Connection, site: SiteConfig, glossary: Glossary, out_dir: Path
) -> TableStats:
    """CSV 保留完整多行 Markdown；JSONL 的标签和质量提示保留为数组。空库也覆盖旧快照。"""
    rows = records(conn, site, glossary)
    csv_path = out_dir / "tables" / f"{site.name}.csv"
    jsonl_path = out_dir / "tables" / f"{site.name}.jsonl"
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=COLUMNS)
    writer.writeheader()
    for row in rows:
        cells = {
            k: json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
            for k, v in row.items()
        }
        writer.writerow(cells)
    # write_if_changed 以 UTF-8 编码；前缀写 U+FEFF 即生成 UTF-8 BOM。
    write_if_changed(csv_path, "\ufeff" + stream.getvalue())
    write_if_changed(jsonl_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return TableStats(len(rows), csv_path, jsonl_path)
