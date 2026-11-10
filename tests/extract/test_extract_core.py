"""extract_full：解码、版权声明块（keep）。"""

from __future__ import annotations

import codecs
from typing import Any

from qbkb.config import SiteConfig
from qbkb.extract import extract_full, load_rules
from qbkb.extract.core import decode_html
from qbkb.extract.rules import ExtractRules

URL = "https://example.test/insights/a"
TEMPLATE = {"name": "t", "body": "#body"}


def _rules(**extract: Any) -> ExtractRules:
    return load_rules(SiteConfig("t", "allowed", {"extract": {"templates": [TEMPLATE], **extract}}))


def _segments(html: str | bytes, **extract: Any) -> list[str]:
    return [s.html for s in extract_full(html, URL, _rules(**extract)).article.segments]


def test_decode_uses_declared_charset() -> None:
    raw = '<html><head><meta charset="windows-1252"></head><body>café</body></html>'
    assert "café" in decode_html(raw.encode("cp1252"))


def test_decode_http_equiv_charset() -> None:
    raw = (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
        "</head><body>naïve</body></html>"
    )
    assert "naïve" in decode_html(raw.encode("latin-1"))


def test_decode_defaults_to_utf8_and_handles_bom_and_bad_names() -> None:
    assert "é" in decode_html("<p>é</p>".encode())
    assert decode_html(codecs.BOM_UTF8 + "<p>é</p>".encode()) == "<p>é</p>"
    assert "é" in decode_html('<meta charset="no-such-codec"><p>é</p>'.encode())


def test_xml_declaration_is_removed_for_lxml() -> None:
    assert decode_html('<?xml version="1.0" encoding="utf-8"?><html></html>') == "<html></html>"


def test_bytes_and_str_give_same_result_for_non_utf8_page() -> None:
    page = (
        '<html><head><meta charset="windows-1252"></head>'
        '<body><div id="body"><p>Prix : 5 €, déjà payé</p></div></body></html>'
    )
    assert _segments(page.encode("cp1252")) == _segments(page) == ["<p>Prix : 5 €, déjà payé</p>"]


def test_keep_is_not_cleaned_by_body_rules() -> None:
    page = (
        '<div id="body"><p>Body text</p><p class="promo">Ad</p></div>'
        '<div id="legal"><p class="promo">Copyright notice</p><p>Drop me? No.</p>'
        '<script>x()</script><p onclick="x()">© Someone</p></div>'
    )
    out = _segments(page, remove=[".promo"], drop_text=["^Drop me"], keep=["#legal"])
    assert out == [
        "<p>Body text</p>",
        "<p>Copyright notice</p>",
        "<p>Drop me? No.</p>",
        "<p>© Someone</p>",
    ]


def test_keep_takes_all_matches_once() -> None:
    page = (
        '<div id="body"><p>Body</p></div>'
        '<div class="legal"><p>First notice</p></div>'
        '<div class="legal"><p>Second notice</p><div class="legal"><p>nested</p></div></div>'
        '<div class="legal"><p>First notice</p></div>'  # 响应式布局里的重复
    )
    out = _segments(page, keep=[".legal"])
    assert out == ["<p>Body</p>", "<p>First notice</p>", "<p>Second notice</p>", "<p>nested</p>"]


def test_keep_rule_with_own_remove_list() -> None:
    page = (
        '<div id="body"><p>Body</p></div>'
        '<h2 id="t"><span class="desk">Notes</span><span class="mobile">Notes</span></h2>'
    )
    out = _segments(page, keep=[{"selector": "#t", "remove": [".mobile"]}])
    assert out == ["<p>Body</p>", "<h2>Notes</h2>"]
