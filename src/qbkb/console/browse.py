"""控制台浏览页 v2 的纯逻辑：文章卡片、质检标签、站点显示名、全文翻译的字符估算。

和 HTTP 无关，server.App 调用它们；估算只读状态库（翻译记忆），不调用任何外部服务。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from qbkb import store
from qbkb.console import sections_ui
from qbkb.translate.fixed import is_interview, untranslated_units
from qbkb.translate.footnotes import sent_length
from qbkb.translate.glossary import Glossary
from qbkb.translate.pipeline import title_unit

MAX_FLAGS = 3
MAX_FLAG_CHARS = 24
# 一次最多选多少篇翻全文
MAX_TRANSLATE_URLS = 50

# 质检提示原文太长或带细节，换成给人看的短标签（前缀匹配）
_FLAG_ALIASES = (("PDF 正文", "PDF 公式表格可能不完整"),)
_CUT = re.compile(r"[：:（(]")


def site_display_name(site: str, firm: object) -> str:
    """站点的显示名：配置里 firm 括号里的简称（Capital Fund Management (CFM) → CFM），
    没有括号就用 firm 全名，再没有就用站点 id。"""
    if isinstance(firm, str) and firm.strip():
        m = re.search(r"\(([^()]+)\)\s*$", firm.strip())
        return m.group(1).strip() if m else firm.strip()
    return site


def flag_labels(qc_issues: str | None) -> list[str]:
    """qc_issues（JSON 数组字符串）里给人看的短标签，去重，最多 MAX_FLAGS 个。"""
    if not qc_issues:
        return []
    try:
        parsed = json.loads(qc_issues)
    except (ValueError, TypeError):
        parsed = [qc_issues]
    items = parsed if isinstance(parsed, list) else [parsed]
    out: list[str] = []
    for item in items:
        text = str(item).strip()
        label = next((to for prefix, to in _FLAG_ALIASES if text.startswith(prefix)), None)
        if label is None:
            label = _CUT.split(text, maxsplit=1)[0].strip()[:MAX_FLAG_CHARS]
        if label and label not in out:
            out.append(label)
        if len(out) >= MAX_FLAGS:
            break
    return out


def article_card(r: Mapping[str, Any] | sqlite3.Row) -> dict[str, object]:
    """浏览页一张文章卡片的数据。"""
    return {
        "url": r["url"],
        "title": r["title_zh"] or r["title"] or r["slug"],
        "title_en": r["title"],
        "date": (r["published"] or "")[:10] or None,
        "summary": r["summary_zh"] or None,
        "translated": bool(r["translated_at"]),
        "has_pdf": bool(r["pdf_path"]),
        "flags": flag_labels(r["qc_issues"]),
    }


def estimate_fulltext_chars(
    conn: sqlite3.Connection, urls: Iterable[str], backend: str, glossary: Glossary
) -> int:
    """估算翻这些文章全文还要多少字符：正文段落 + 标题，扣掉固定译文和翻译记忆已命中的，
    跨文章去重。按送出前的长度算（DeepL 脚注占位展开后），是上限：实际可能因后端而略有出入。
    """
    units: list[str] = []
    for url in urls:
        row = store.get_article(conn, url)
        if row is None:
            continue
        segments = store.get_segments(conn, url)
        skip = untranslated_units(segments, glossary.fixed, interview=is_interview(row["tags"]))
        units += [seg.html for seg in segments if seg.src_hash not in skip]
        title = row["title"]
        if title and str(title).strip():
            units.append(title_unit(str(title)))
    missing = sections_ui.missing_units(conn, units, backend, glossary)
    return sum(sent_length(u) for u in missing)
