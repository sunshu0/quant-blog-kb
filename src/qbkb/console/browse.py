"""控制台浏览页 v2 的纯逻辑：文章卡片、质检标签、站点显示名、全文翻译的字符估算。

和 HTTP 无关，server.App 调用它们；估算只读状态库（翻译记忆），不调用任何外部服务。
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from qbkb import store
from qbkb.models import segment_hash
from qbkb.translate.fixed import is_interview, untranslated_units
from qbkb.translate.footnotes import sent_length
from qbkb.translate.glossary import Glossary
from qbkb.translate.pipeline import lookup_translation, title_unit

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


@dataclass(frozen=True, slots=True)
class FulltextEstimate:
    """翻选中文章全文的字符估算。

    base：按流水线（translate_article）同样的规则算出的基础字符数，不含结构校验失败后的重试；
    maximum：再加上"每个待翻正文段落都校验失败、重翻一次"的最坏情况
    （重翻单独送一次、不带 context）。
    没有待翻正文段落时只剩标题（标题不重试），base 就是确切值。
    """

    base: int
    maximum: int

    @property
    def exact(self) -> bool:
        return self.base == self.maximum


def estimate_fulltext(
    conn: sqlite3.Connection, urls: Iterable[str], backend: str, glossary: Glossary
) -> FulltextEstimate:
    """估算翻这些文章全文要送出多少字符，规则和 translate_article 一致（--retry-rejected 关）：

    - 正文段落：固定译文 / 说话人标签不送；翻译记忆里有记录的不送（含 rejected：流水线不自动重翻，
      留给人工复查）；没有记录的才送。跨文章按 hash 去重（先翻的写进翻译记忆，后面的命中）；
    - 标题：只有翻译记忆里有**可用**译文才免费，rejected 的照样送；和某个待翻正文段落相同的
      标题单元也免费（正文先翻完，标题命中）；
    - 按送出前的长度算（DeepL 脚注占位展开后）。
    """
    body: dict[str, str] = {}  # hash → 待翻正文段落
    titles: dict[str, None] = {}
    for url in urls:
        row = store.get_article(conn, url)
        if row is None:
            continue
        segments = store.get_segments(conn, url)
        skip = untranslated_units(segments, glossary.fixed, interview=is_interview(row["tags"]))
        for seg in segments:
            if seg.src_hash in skip or seg.src_hash in body:
                continue
            if store.tm_lookup(conn, seg.src_hash, backend, glossary.version_for(seg.html)):
                continue
            body[seg.src_hash] = seg.html
        title = row["title"]
        if title and str(title).strip():
            titles[title_unit(str(title))] = None
    body_chars = sum(sent_length(h) for h in body.values())
    title_chars = sum(
        sent_length(u)
        for u in titles
        if segment_hash(u) not in body and lookup_translation(conn, u, backend, glossary) is None
    )
    return FulltextEstimate(base=body_chars + title_chars, maximum=2 * body_chars + title_chars)


def url_problem(url: str) -> str | None:
    """网址不能带控制字符（含换行、NUL）、行分隔符，也不能首尾有空白或以 # 开头：
    网址清单是一行一个，这些字符会把一个网址拆成几行，或者被当成注释行。"""
    if url != url.strip() or url.startswith("#"):
        return "网址首尾不能有空白，也不能以 # 开头"
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in url):
        return "网址里不能有换行、控制字符"
    return None
