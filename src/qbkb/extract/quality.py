"""抽取质检：把可疑的结果标出来给人看，不自动丢弃（design.md 4.3）。"""

from __future__ import annotations

import re

from qbkb.extract.clean import block_text
from qbkb.extract.core import FALLBACK, Extraction
from qbkb.extract.rules import ExtractRules


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """整词、不分大小写匹配：关键词 'cookie' 不会命中正文里的 'cookies'。"""
    words = r"\s+".join(re.escape(w) for w in keyword.split())
    return re.compile(rf"(?<!\w){words}(?!\w)", re.IGNORECASE)


def check_quality(
    result: Extraction, rules: ExtractRules, *, total_chars: int | None = None
) -> list[str]:
    """返回问题列表；空列表表示没发现问题。只检查正文，不检查附在后面的版权声明。

    total_chars：正文总字符数。缺省从网页正文段落算；接上 PDF 全文时，调用方把网页正文 +
    PDF 全文的总字符数传进来，避免短摘要被误报「正文太短」（正文太短按接上 PDF 后的总正文判断）。
    """
    issues: list[str] = []
    meta = result.article.meta
    body = result.article.segments[: result.n_body]
    texts = [block_text(s.html) for s in body]

    chars = sum(len(t) for t in texts) if total_chars is None else total_chars
    if chars < rules.min_chars:
        issues.append(f"正文太短：{chars} 字符（下限 {rules.min_chars}）")
    for kw in rules.noise_keywords:
        pattern = keyword_pattern(kw)
        hits = [i for i, t in enumerate(texts) if pattern.search(t)]
        if hits:
            idx = ", ".join(str(body[i].idx) for i in hits)
            issues.append(f"出现噪音关键词 {kw!r}：段落 {idx}")
    if not meta.title:
        issues.append("缺少标题")
    if not meta.published:
        issues.append("缺少发布日期")
    if rules.keep and not result.keep_found:
        issues.append("没有找到版权声明（extract.keep）")
    if rules.templates and result.method == FALLBACK:
        issues.append("没有匹配的站点规则，用了 trafilatura 兜底")
    return issues
