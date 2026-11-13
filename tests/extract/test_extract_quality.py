from __future__ import annotations

from typing import Any

from qbkb.config import SiteConfig
from qbkb.extract import check_quality, extract_full, load_rules
from qbkb.extract.quality import keyword_pattern
from qbkb.extract.rules import ExtractRules

URL = "https://example.test/insights/a"
LONG = "word " * 200


def _rules(**extract: Any) -> ExtractRules:
    return load_rules(SiteConfig("t", "allowed", {"extract": extract}))


def _page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


TEMPLATE = {"name": "t", "body": "#body", "title": "h1", "published": ".date"}


def test_clean_article_has_no_issues() -> None:
    rules = _rules(templates=[TEMPLATE], keep=["#legal"])
    html = _page(
        f'<h1>T</h1><p class="date">2024-01-01</p><div id="body"><p>{LONG}</p></div>'
        '<div id="legal"><p>© someone</p></div>'
    )
    assert check_quality(extract_full(html, URL, rules), rules) == []


def test_short_noisy_and_missing_meta() -> None:
    rules = _rules(templates=[TEMPLATE], keep=["#legal"], min_chars=100)
    html = _page('<div id="body"><p>Please ACCEPT  cookies</p><p>ok</p></div>')
    issues = check_quality(extract_full(html, URL, rules), rules)
    assert issues == [
        "正文太短：23 字符（下限 100）",
        "出现噪音关键词 'accept cookies'：段落 0",
        "缺少标题",
        "缺少发布日期",
        "没有找到版权声明（extract.keep）",
    ]


def test_keyword_in_copyright_block_is_not_noise() -> None:
    rules = _rules(templates=[TEMPLATE], keep=["#legal"], min_chars=0)
    html = _page(
        '<h1>T</h1><p class="date">2024-01-01</p><div id="body"><p>fine</p></div>'
        '<div id="legal"><p>We use cookies.</p></div>'
    )
    assert check_quality(extract_full(html, URL, rules), rules) == []


def test_fallback_is_reported_when_site_has_rules() -> None:
    rules = _rules(templates=[{**TEMPLATE, "require": "#body"}], min_chars=0)
    html = _page(
        f"<article><h1>T</h1><p>{LONG}</p><p>{LONG}</p></article>",
        '<meta property="article:published_time" content="2024-01-01">',
    )
    result = extract_full(html, URL, rules)
    assert result.method == "trafilatura"
    assert "没有匹配的站点规则，用了 trafilatura 兜底" in check_quality(result, rules)


def test_keyword_matches_whole_words_only() -> None:
    p = keyword_pattern("cookie")
    assert p.search("a Cookie here")
    assert p.search("cookie.")
    assert not p.search("eat less cookies")
    assert not p.search("fortunecookie")
    assert keyword_pattern("You are leaving").search("you  are\nleaving now")


def test_total_chars_override_skips_short_body_flag() -> None:
    """接上 PDF 全文后，正文太短按总字符数判断：短摘要 + PDF 全文不再报「正文太短」。"""
    rules = _rules(templates=[TEMPLATE], min_chars=1000)
    html = _page('<h1>T</h1><p class="date">2024-01-01</p><div id="body"><p>short</p></div>')
    result = extract_full(html, URL, rules)
    assert "正文太短" in check_quality(result, rules)[0]
    issues = check_quality(result, rules, total_chars=5000)
    assert "正文太短" not in " ".join(issues)
