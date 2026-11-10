import gzip
from pathlib import Path

import pytest

from qbkb.discover import MAX_SITEMAP_BYTES, SitemapEntry, SitemapError, parse_sitemap

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_urlset_with_namespace() -> None:
    sm = parse_sitemap(_read("sitemap_insights.xml"))
    assert sm.sitemaps == []
    assert sm.urls[0] == SitemapEntry(
        loc="https://www.example.test/insights/alpha-signals", lastmod="2026-01-02"
    )
    # 空 <loc> 被丢弃；xhtml:link 不当成 URL
    assert len(sm.urls) == 11
    assert all(e.loc for e in sm.urls)


def test_loc_and_lastmod_are_stripped() -> None:
    sm = parse_sitemap(_read("sitemap_insights.xml"))
    assert sm.urls[1].loc == "https://WWW.EXAMPLE.TEST/insights/beta-carry#top"


def test_urlset_without_namespace() -> None:
    sm = parse_sitemap(_read("sitemap_pages_no_ns.xml"))
    assert [e.loc for e in sm.urls] == [
        "https://www.example.test/insights/delta-volatility",
        "https://www.example.test/insights/report.pdf",
    ]
    assert sm.urls[0].lastmod == "2026-02-01T08:00:00+00:00"
    assert sm.urls[1].lastmod is None


def test_sitemap_index() -> None:
    sm = parse_sitemap(_read("sitemap_index.xml"))
    assert sm.urls == []
    assert [e.loc for e in sm.sitemaps] == [
        "https://www.example.test/sitemap-insights.xml",
        "https://www.example.test/sitemap-pages.xml",
        "https://cdn.other.test/sitemap.xml",
        "https://www.example.test/sitemap-insights.xml",
    ]


def test_gzip_sitemap_is_decompressed() -> None:
    raw = _read("sitemap_pages_no_ns.xml")
    assert parse_sitemap(gzip.compress(raw)) == parse_sitemap(raw)


def test_malformed_xml_raises() -> None:
    with pytest.raises(SitemapError):
        parse_sitemap(b"<urlset><url><loc>x</loc></url>")


def test_unknown_root_raises() -> None:
    with pytest.raises(SitemapError):
        parse_sitemap(b"<html><body>not a sitemap</body></html>")


def test_entities_are_not_expanded() -> None:
    data = (
        b'<?xml version="1.0"?>'
        b'<!DOCTYPE urlset [<!ENTITY host "https://evil.test/">]>'
        b"<urlset><url><loc>&host;insights/a</loc></url></urlset>"
    )
    sm = parse_sitemap(data)
    assert not any(e.loc.startswith("https://evil.test") for e in sm.urls)


# ---- 大小上限与损坏的 gzip ----


def test_gzip_bomb_is_rejected() -> None:
    bomb = gzip.compress(b"<urlset>" + b" " * 5000 + b"</urlset>")
    assert len(bomb) < 1000
    with pytest.raises(SitemapError, match="解压后超过"):
        parse_sitemap(bomb, max_bytes=1000)


def test_oversized_raw_sitemap_is_rejected() -> None:
    with pytest.raises(SitemapError, match="超过"):
        parse_sitemap(b"<urlset>" + b" " * 2000 + b"</urlset>", max_bytes=1000)


def test_default_limit_is_50_mb() -> None:
    assert MAX_SITEMAP_BYTES == 50 * 1024 * 1024


@pytest.mark.parametrize(
    "data",
    [
        bytes((0x1F, 0x8B)) + b"garbage-after-magic",  # BadGzipFile
        gzip.compress(b"<urlset></urlset>")[:-12],  # 截断：EOFError
        gzip.compress(b"<urlset></urlset>")[:12] + b"corrupt-deflate-stream" * 3,  # zlib.error
    ],
)
def test_corrupt_gzip_raises_sitemap_error(data: bytes) -> None:
    with pytest.raises(SitemapError, match="gzip"):
        parse_sitemap(data)
