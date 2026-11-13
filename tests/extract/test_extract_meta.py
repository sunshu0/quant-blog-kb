from __future__ import annotations

import json
from typing import Any

import pytest
from lxml.html import document_fromstring

from qbkb.config import SiteConfig
from qbkb.extract.meta import extract_meta, normalize_date
from qbkb.extract.rules import DEFAULT_DATE_FORMATS, ExtractRules, load_rules
from qbkb.models import ArticleMeta

URL = "https://example.test/insights/a"


def _rules(extract: dict[str, Any] | None = None) -> ExtractRules:
    return load_rules(SiteConfig("t", "allowed", {"extract": extract or {}}))


def _page(head: str = "", body: str = "<p>x</p>") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _ld(obj: Any) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def _meta(html: str, extract: dict[str, Any] | None = None) -> ArticleMeta:
    rules = _rules(extract)
    template = rules.templates[0] if rules.templates else None
    return extract_meta(document_fromstring(html), html, URL, rules, template)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2020-12-03T00:00:00+01:00", "2020-12-03"),
        ("2020-12-03", "2020-12-03"),
        ("3 December 2020", "2020-12-03"),
        (" 3  December 2020 ", "2020-12-03"),
        ("December 3, 2020", "2020-12-03"),
        ("3 Dec 2020", "2020-12-03"),
        ("Coming soon", None),
        ("2023-00-00", None),
        ("2023-02-30T00:00:00Z", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_date(raw: str | None, expected: str | None) -> None:
    assert normalize_date(raw, DEFAULT_DATE_FORMATS) == expected


def test_jsonld_graph_with_author_reference() -> None:
    ld = {
        "@graph": [
            {
                "@type": "Article",
                "headline": "H",
                "datePublished": "2024-01-02T00:00:00Z",
                "author": [{"name": "A"}, {"@id": "#b"}, "C"],
                "keywords": ["k1", "k2"],
            },
            {"@type": "Person", "@id": "#b", "name": "B"},
        ]
    }
    m = _meta(_page(_ld(ld)))
    assert (m.title, m.author, m.published, m.tags) == ("H", "A, B, C", "2024-01-02", ("k1", "k2"))


def test_jsonld_list_at_top_level_and_type_list() -> None:
    ld = [{"@type": "WebSite"}, {"@type": ["NewsArticle"], "headline": "N", "author": "X"}]
    m = _meta(_page(_ld(ld)))
    assert (m.title, m.author) == ("N", "X")


def test_broken_jsonld_is_skipped() -> None:
    head = '<script type="application/ld+json">{oops</script>' + _ld(
        {"@type": "Article", "headline": "OK"}
    )
    assert _meta(_page(head)).title == "OK"


def test_meta_tags_when_no_jsonld() -> None:
    head = (
        '<meta property="og:title" content="T | Site">'
        '<meta property="article:published_time" content="2021-05-06T10:00:00Z">'
        '<meta name="author" content="M">'
        '<meta property="article:tag" content="a"><meta property="article:tag" content="b">'
    )
    m = _meta(_page(head), {"title_strip": r"\s*\|\s*Site$"})
    assert (m.title, m.author, m.published, m.tags) == ("T", "M", "2021-05-06", ("a", "b"))


def test_keywords_meta_is_split() -> None:
    m = _meta(_page('<meta name="keywords" content="x, y ,x">'))
    assert m.tags == ("x", "y")


def test_site_rule_fields_with_pattern() -> None:
    body = (
        '<h1 class="t">Rule title</h1><p class="d">7 May 2024</p>'
        '<span class="a">P</span><span class="a">Q</span><p class="g">VIDEO | 3 MIN</p>'
    )
    extract = {
        "templates": [
            {
                "name": "x",
                "body": "body",
                "title": ".t",
                "published": ".d",
                "author": ".a",
                "tags": {"selector": ".g", "pattern": r"^\s*([^|]+?)\s*(?:\||$)"},
            }
        ]
    }
    m = _meta(_page(body=body), extract)
    assert (m.title, m.author, m.published, m.tags) == (
        "Rule title",
        "P, Q",
        "2024-05-07",
        ("VIDEO",),
    )


def test_jsonld_wins_over_site_rule() -> None:
    head = _ld({"@type": "Article", "headline": "From LD", "datePublished": "2020-01-01"})
    extract = {"templates": [{"name": "x", "body": "body", "title": "h1", "published": "p"}]}
    m = _meta(_page(head, "<h1>From rule</h1><p>2 February 2022</p>"), extract)
    assert (m.title, m.published) == ("From LD", "2020-01-01")


def test_title_falls_back_to_title_tag() -> None:
    m = _meta(_page("<title>Only Title | Site</title>"), {"title_strip": r"\s*\|\s*Site$"})
    assert m.title == "Only Title"


def test_invalid_jsonld_date_falls_through_to_next_source() -> None:
    head = _ld({"@type": "Article", "headline": "H", "datePublished": "2023-00-00"})
    head += '<meta property="article:published_time" content="2023-04-05">'
    assert _meta(_page(head)).published == "2023-04-05"


# ---- WordPress / Yoast 的 JSON-LD（CFM 等站点） ----


def test_jsonld_html_entities_are_decoded() -> None:
    head = _ld(
        {
            "@type": "Article",
            "headline": "&#8220;Quoted&#8221; title &amp; more",
            "author": {"name": "Jane &amp; John"},
            "keywords": ["Rates &amp; FX"],
        }
    )
    m = _meta(_page(head))
    assert m.title == "“Quoted” title & more"
    assert m.author == "Jane & John"
    assert m.tags == ("Rates & FX",)


def test_article_section_goes_first_in_tags() -> None:
    head = _ld(
        {
            "@type": "Article",
            "headline": "H",
            "articleSection": ["Commentary &amp; Insights", "Markets, Rates"],
            "keywords": "Inflation, Macro",
        }
    )
    # 栏目名不按逗号拆；关键词照旧按逗号拆
    assert _meta(_page(head)).tags == (
        "Commentary & Insights",
        "Markets, Rates",
        "Inflation",
        "Macro",
    )


def test_article_section_as_string() -> None:
    head = _ld({"@type": "Article", "headline": "H", "articleSection": "Whitepaper"})
    assert _meta(_page(head)).tags == ("Whitepaper",)


def test_publisher_name_and_urls_are_not_authors() -> None:
    """WordPress 账号名就是机构名、主题写错的 meta author 是网址：都跳过，用站点规则。"""
    head = '<meta property="og:site_name" content="ACME">'
    head += _ld(
        {
            "@graph": [
                {
                    "@type": "Article",
                    "headline": "H",
                    "author": {"name": "acme"},
                    "publisher": {"@id": "#org"},
                },
                {"@type": "Organization", "@id": "#org", "name": "ACME Capital"},
            ]
        }
    )
    head += '<meta name="author" content="ACME Capital">'
    head += '<meta name="author" content="https://theme.example">'
    extract = {
        "templates": [
            {"name": "x", "body": "body", "author": {"selector": "li", "pattern": r"^([^\n]+)"}}
        ]
    }
    body = "<ul><li>Ann Real<br/>Head of Research</li><li></li><li>Bo Real<br>Univ</li></ul>"
    assert _meta(_page(head, body), extract).author == "Ann Real, Bo Real"


def test_real_jsonld_author_still_wins() -> None:
    head = '<meta property="og:site_name" content="ACME">'
    head += _ld({"@type": "Article", "headline": "H", "author": {"name": "Real Person"}})
    extract = {"templates": [{"name": "x", "body": "body", "author": "li"}]}
    assert _meta(_page(head, "<ul><li>Other</li></ul>"), extract).author == "Real Person"


def test_rule_value_br_becomes_line_break() -> None:
    body = "<ul><li> Ann  Real <br/> Head, <b>Desk</b></li></ul>"
    # 不带 pattern：<br> 两边的字不会粘在一起
    plain = {"templates": [{"name": "x", "body": "body", "author": "li"}]}
    assert _meta(_page(body=body), plain).author == "Ann Real Head, Desk"
    first = {
        "templates": [
            {"name": "x", "body": "body", "author": {"selector": "li", "pattern": r"^([^\n]+)"}}
        ]
    }
    assert _meta(_page(body=body), first).author == "Ann Real"


def test_section_does_not_hide_meta_keywords() -> None:
    head = _ld({"@type": "Article", "headline": "H", "articleSection": "Research"})
    head += '<meta property="article:tag" content="Rates">'
    assert _meta(_page(head)).tags == ("Research", "Rates")


def test_section_does_not_hide_rule_keywords() -> None:
    head = _ld({"@type": "Article", "headline": "H", "articleSection": "Research"})
    rules = {"templates": [{"name": "x", "body": "body", "tags": ".tag"}]}
    assert _meta(_page(head, '<span class="tag">Macro</span>'), rules).tags == ("Research", "Macro")
