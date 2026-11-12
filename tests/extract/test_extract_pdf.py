"""PDF 正文抽取：pypdf 取文字、去页眉页脚页码、连字符合并、接进 segments。全部离线。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from pdf_helpers import make_pdf

from qbkb import store
from qbkb.extract.pdf import NoTextLayerError, PdfExtractError, extract_pdf_text
from qbkb.extract.pipeline import extract_one
from qbkb.extract.rules import ExtractRules
from qbkb.models import DiscoveredUrl, Status
from qbkb.pdf import PdfRules

# ---- extract_pdf_text：纯函数 ----


def test_paragraphs_join_lines_and_merge_hyphens() -> None:
    pdf = make_pdf(["The model is incre-\nmental and robust."])
    assert extract_pdf_text(pdf) == ["The model is incremental and robust."]


def test_header_and_page_number_removed() -> None:
    pages = [
        "Alpha Report\n\nAbstract body line one\n\nConclusion alpha\nPage 1",
        "Alpha Report\n\nMore body on page two\n\nContinuing analysis\nPage 2",
        "Alpha Report\n\nFinal section here\n\nReferences listed\nPage 3",
    ]
    out = extract_pdf_text(make_pdf(pages))
    joined = " ".join(out)
    assert "Alpha Report" not in joined
    assert "Page" not in joined
    assert "Abstract body line one" in joined
    assert "References listed" in joined


def test_page_numbered_footer_removed() -> None:
    """页脚常带页码（"www.site.com 02"），去掉页码后按跨页重复检测出来。"""
    pages = [
        "Site Report\n\nAbstract text on the first page.\n\nFirst body paragraph.\nwww.site.com 01",
        "Site Report\n\nSecond body paragraph here.\n\nMore analysis text.\nwww.site.com 02",
        "Site Report\n\nThird body paragraph.\n\nConclusion of the report.\nwww.site.com 03",
    ]
    out = extract_pdf_text(make_pdf(pages))
    joined = " ".join(out)
    assert "Site Report" not in joined
    assert "www.site.com" not in joined
    assert "First body paragraph" in joined
    assert "Conclusion of the report" in joined


def test_page_boundary_is_paragraph_boundary() -> None:
    pdf = make_pdf(["First page text", "Second page text"])
    assert extract_pdf_text(pdf) == ["First page text", "Second page text"]


def test_no_text_layer_raises() -> None:
    with pytest.raises(NoTextLayerError):
        extract_pdf_text(make_pdf([""]))


def test_corrupt_pdf_raises() -> None:
    with pytest.raises(PdfExtractError):
        extract_pdf_text(b"this is not a pdf at all")


def test_long_paragraph_splits_at_sentences() -> None:
    long = ("Sentence number one. " * 200).strip()
    out = extract_pdf_text(make_pdf([long]))
    assert len(out) > 1
    assert max(len(p) for p in out) <= 2000


# ---- extract_one：接进 segments ----

SHORT_HTML = (
    b"<html><head><title>A Paper</title></head>"
    b"<body><article><p>Short abstract of the paper.</p></article></body></html>"
)
LONG_HTML = (
    b"<html><head><title>A Paper</title></head><body><article><p>"
    + b"word " * 400
    + b"</p></article></body></html>"
)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def _article(
    conn: sqlite3.Connection,
    tmp_path: Path,
    url: str,
    html: bytes,
    pdf_bytes: bytes | None,
) -> None:
    slug = url.rsplit("/", 1)[-1]
    raw = tmp_path / "raw" / "example"
    raw.mkdir(parents=True)
    html_path = raw / f"{slug}.html"
    html_path.write_bytes(html)
    pdf_path: str | None = None
    if pdf_bytes is not None:
        pdf = raw / f"{slug}.pdf"
        pdf.write_bytes(pdf_bytes)
        pdf_path = pdf.as_posix()
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug=slug))
    store.set_status(conn, url, Status.FETCHED, raw_path=html_path.as_posix(), pdf_path=pdf_path)


URL = "https://www.example.test/papers/alpha"
PDF_PARAS = make_pdf(
    [
        "Section one\n\nFirst full paragraph of the body.\n\nSecond body paragraph.",
        "Section two\n\nThird body paragraph here.",
    ]
)


def test_short_body_uses_pdf_and_keeps_summary(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _article(conn, tmp_path, URL, SHORT_HTML, PDF_PARAS)
    rules = ExtractRules(site="example")
    pdf_rules = PdfRules(".pdf a", min_chars=100)
    extract_one(conn, URL, rules, root=tmp_path, pdf_rules=pdf_rules)

    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.EXTRACTED
    segments = store.get_segments(conn, URL)
    first = segments[0].html
    assert "Short abstract of the paper." in first
    joined = " ".join(s.html for s in segments)
    assert "First full paragraph of the body." in joined
    assert "Third body paragraph here." in joined
    assert row["body_segments"] == len(segments)  # 没有 keep 块，全部是正文
    issues = json.loads(row["qc_issues"] or "[]")
    assert any("PDF 正文" in i for i in issues)


def test_long_body_ignores_pdf(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _article(conn, tmp_path, URL, LONG_HTML, PDF_PARAS)
    rules = ExtractRules(site="example")
    pdf_rules = PdfRules(".pdf a", min_chars=100)
    extract_one(conn, URL, rules, root=tmp_path, pdf_rules=pdf_rules)
    segments = store.get_segments(conn, URL)
    joined = " ".join(s.html for s in segments)
    assert "First full paragraph of the body." not in joined
    assert "word" in joined


def test_short_body_without_pdf_falls_back_normally(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _article(conn, tmp_path, URL, SHORT_HTML, None)
    rules = ExtractRules(site="example")
    extract_one(conn, URL, rules, root=tmp_path, pdf_rules=PdfRules(".pdf a", min_chars=100))
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.EXTRACTED
    segments = store.get_segments(conn, URL)
    assert len(segments) == 1
    assert "Short abstract" in segments[0].html


def test_scanned_pdf_fails_extract(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _article(conn, tmp_path, URL, SHORT_HTML, make_pdf([""]))
    rules = ExtractRules(site="example")
    with pytest.raises(NoTextLayerError):
        extract_one(conn, URL, rules, root=tmp_path, pdf_rules=PdfRules(".pdf a", min_chars=100))
