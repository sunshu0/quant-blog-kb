"""PDF 正文下载：站点配置、找链接、跨站拦截、断点续跑。全部离线，不联网。"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from scrapy.http import Request, TextResponse

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.fetch import select_pdf_targets
from qbkb.fetch.outcome import Kind
from qbkb.fetch.spider import ArticleSpider
from qbkb.models import DiscoveredUrl, Status
from qbkb.pdf import PdfRules, find_pdf_url, load_pdf_rules, pdf_raw_path

BASE = "https://www.example.test/papers/alpha"
PDF_URL = "https://www.example.test/wp-content/uploads/report.pdf"
HTML_WITH_PDF = (
    b'<html><body><aside class="sidebar"><ul class="file-list">'
    b'<a href="/wp-content/uploads/report.pdf">Download PDF</a></ul></aside></body></html>'
)


def _site(pdf: dict | None = None) -> SiteConfig:
    raw: dict = {"base_url": "https://www.example.test"}
    if pdf is not None:
        raw["pdf"] = pdf
    return SiteConfig("example", "allowed", raw)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


# ---- 站点配置 ----


def test_no_pdf_section_disables_feature() -> None:
    assert load_pdf_rules(_site()) is None


def test_load_pdf_rules_reads_all_fields() -> None:
    rules = load_pdf_rules(_site({"selector": ".file-list a", "min_chars": 1200, "max_size_mb": 8}))
    assert rules is not None
    assert rules.selector == ".file-list a"
    assert rules.min_chars == 1200
    assert rules.max_size_bytes == 8 * 1024 * 1024


def test_load_pdf_rules_defaults() -> None:
    rules = load_pdf_rules(_site({"selector": ".file-list a"}))
    assert rules is not None
    assert rules.min_chars == 500
    assert rules.max_size_bytes == 20 * 1024 * 1024


@pytest.mark.parametrize(
    "pdf",
    [
        {"selector": ""},
        {"selector": "not(valid"},
        {"selector": ".a", "min_chars": -1},
        {"selector": ".a", "min_chars": True},
        {"selector": ".a", "max_size_mb": 0},
    ],
)
def test_bad_pdf_config_raises(pdf: dict) -> None:
    with pytest.raises(ValueError):
        load_pdf_rules(_site(pdf))


# ---- 找链接 ----


def test_find_pdf_url_returns_absolute_same_site() -> None:
    rules = PdfRules(".sidebar .file-list a")
    assert find_pdf_url(HTML_WITH_PDF, BASE, rules) == PDF_URL


def test_find_pdf_url_rejects_cross_site() -> None:
    rules = PdfRules(".sidebar .file-list a")
    cross = HTML_WITH_PDF.replace(
        b"/wp-content/uploads/report.pdf", b"https://cdn.other.test/x.pdf"
    )
    assert find_pdf_url(cross, BASE, rules) is None


def test_find_pdf_url_none_when_no_match() -> None:
    rules = PdfRules(".missing")
    assert find_pdf_url(HTML_WITH_PDF, BASE, rules) is None


def test_pdf_raw_path_rejects_unsafe_slug(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        pdf_raw_path(tmp_path, "example", "../etc")


# ---- 断点续跑：挑还没下载 PDF 的文章 ----


def _fetched(conn: sqlite3.Connection, url: str, *, pdf_path: str | None = None) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug="alpha"))
    store.set_status(
        conn, url, Status.FETCHED, raw_path="raw/example/alpha.html", pdf_path=pdf_path
    )


def test_select_pdf_targets_picks_fetched_without_pdf(conn: sqlite3.Connection) -> None:
    _fetched(conn, BASE)
    assert select_pdf_targets(conn, "example") == [BASE]


def test_select_pdf_targets_skips_already_downloaded(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    pdf = tmp_path / "alpha.pdf"
    pdf.write_bytes(b"%PDF")
    _fetched(conn, BASE, pdf_path=pdf.as_posix())
    assert select_pdf_targets(conn, "example") == []


def test_select_pdf_targets_skips_missing_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _fetched(conn, BASE, pdf_path=str(tmp_path / "gone.pdf"))
    assert select_pdf_targets(conn, "example") == [BASE]


def test_select_pdf_targets_ignores_other_status_and_site(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _fetched(conn, BASE)
    published = "https://www.example.test/papers/beta"
    store.upsert_discovered(conn, DiscoveredUrl(url=published, site="example", slug="beta"))
    store.set_status(conn, published, Status.PUBLISHED, raw_path="raw/example/beta.html")
    other = "https://other.test/x"
    store.upsert_discovered(conn, DiscoveredUrl(url=other, site="other", slug="x"))
    store.set_status(conn, other, Status.FETCHED, raw_path="raw/other/x.html")
    assert select_pdf_targets(conn, "example") == [BASE]


# ---- 爬虫：抓到 HTML 后调度 PDF 下载 ----


_DEFAULT = object()


def _spider(
    conn: sqlite3.Connection,
    tmp_path: Path,
    *,
    pdf_rules: object = _DEFAULT,
    pdf_counts: Counter[str] | None = None,
) -> ArticleSpider:
    if pdf_rules is _DEFAULT:
        pdf_rules = PdfRules(".sidebar .file-list a")
    return ArticleSpider(
        conn=conn,
        urls=[],
        raw_dir=tmp_path / "raw",
        allowed_host="www.example.test",
        counts=Counter(),
        pdf_rules=pdf_rules,  # type: ignore[arg-type]
        pdf_counts=pdf_counts,
    )


def test_parse_article_schedules_pdf_request(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    spider = _spider(conn, tmp_path)
    request = Request(BASE, cb_kwargs={"article_url": BASE})
    resp = TextResponse(request.url, status=200, body=HTML_WITH_PDF, request=request)
    scheduled = spider.parse_article(resp, article_url=BASE)
    assert isinstance(scheduled, Request)
    assert scheduled.url == PDF_URL
    assert scheduled.cb_kwargs == {"article_url": BASE}


def test_parse_article_no_pdf_when_not_configured(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    spider = _spider(conn, tmp_path, pdf_rules=None)
    request = Request(BASE, cb_kwargs={"article_url": BASE})
    resp = TextResponse(request.url, status=200, body=HTML_WITH_PDF, request=request)
    assert spider.parse_article(resp, article_url=BASE) is None


def _pdf_response(url: str, body: bytes, status: int = 200) -> TextResponse:
    return TextResponse(
        url,
        status=status,
        body=body,
        headers={"Content-Type": "application/pdf"},
        request=Request(url, cb_kwargs={"article_url": BASE}),
    )


def test_parse_pdf_writes_raw_and_sets_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    counts: Counter[str] = Counter()
    spider = _spider(conn, tmp_path, pdf_counts=counts)
    spider.parse_pdf(_pdf_response(PDF_URL, b"%PDF-1.7 fake body"), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FETCHED
    assert Path(row["pdf_path"]).is_file()
    assert Path(row["pdf_path"]).read_bytes() == b"%PDF-1.7 fake body"
    assert counts == Counter({"ok": 1})


def test_parse_pdf_failure_records_retryable_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    counts: Counter[str] = Counter()
    spider = _spider(conn, tmp_path, pdf_counts=counts)
    spider.parse_pdf(_pdf_response(PDF_URL, b"", status=404), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert row["error"] == "pdf: HTTP 404"
    assert row["pdf_path"] is None
    assert counts == Counter({"failed": 1})


def test_parse_pdf_rejects_non_pdf_body(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    spider = _spider(conn, tmp_path)
    spider.parse_pdf(_pdf_response(PDF_URL, b"<html>not a pdf</html>"), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert row["error"] == "pdf: 不是 PDF（内容类型 application/pdf）"


def test_pdf_download_failure_counts_as_pdf_not_kind_failed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """PDF 下载失败单独统计，不混进 HTML 抓取的 Kind.FAILED。"""
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    spider = _spider(conn, tmp_path)
    assert spider.counts[Kind.FAILED] == 0
    spider.parse_pdf(_pdf_response(PDF_URL, b"", status=500), article_url=BASE)
    assert spider.counts[Kind.FAILED] == 0


def test_pdf_retry_picks_failed_and_recovers_to_fetched(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """PDF 下载失败 → failed（pdf: 前缀）；重跑时被选中，下载成功后回到 fetched。"""
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    spider = _spider(conn, tmp_path)
    spider.parse_pdf(_pdf_response(PDF_URL, b"", status=404), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None and row["status"] == Status.FAILED

    # 断点续跑：failed 且 raw_path 还在、PDF 还没下载的文章会被再次选中
    assert select_pdf_targets(conn, "example") == [BASE]
    spider.parse_pdf(_pdf_response(PDF_URL, b"%PDF-1.7 ok"), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FETCHED
    assert row["error"] is None
    assert row["pdf_path"] is not None
    assert select_pdf_targets(conn, "example") == []
