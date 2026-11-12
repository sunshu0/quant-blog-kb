"""PDF 正文下载：站点配置、找链接、跨站拦截、断点续跑。全部离线，不联网。"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse, Request, TextResponse

from qbkb import config, store
from qbkb.config import SiteConfig
from qbkb.extract.rules import ExtractRules, load_rules
from qbkb.fetch import run_fetch, select_pdf_targets
from qbkb.fetch.outcome import Kind
from qbkb.fetch.process import META_RETRY_AFTER_REFUSED, process_response
from qbkb.fetch.spider import (
    META_STRICT_HOST,
    ArticleSpider,
    HostAllowlistMiddleware,
)
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
    extract_rules: ExtractRules | None = None,
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
        extract_rules=extract_rules,
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


# ---- P1：PDF 跨站重定向 / 最终响应同源校验 ----

# 跨站重定向（含 meta refresh）由复用的 HostAllowlistMiddleware 在每个请求发出前拦下；
# PDF 额外要求与文章严格同 host（比白名单允许子域更严），在 parse_pdf 里对最终响应再校验一次。


def test_parse_pdf_rejects_cross_site_response(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    counts: Counter[str] = Counter()
    spider = _spider(conn, tmp_path, pdf_counts=counts)
    cross = "https://cdn.other.test/x.pdf"
    spider.parse_pdf(_pdf_response(cross, b"%PDF-1.7 x"), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert row["error"] == f"pdf: PDF 跨站（最终响应 {cross}）"
    assert row["pdf_path"] is None
    assert counts == Counter({"failed": 1})


# ---- P2：PDF 长 Retry-After 也要停止整站 ----


def test_parse_pdf_long_retry_after_records_failure(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    spider = _spider(conn, tmp_path)
    request = Request(
        PDF_URL, meta={META_RETRY_AFTER_REFUSED: 3600.0}, cb_kwargs={"article_url": BASE}
    )
    resp = TextResponse(
        PDF_URL,
        status=429,
        body=b"",
        headers={"Content-Type": "application/pdf"},
        request=request,
    )
    # 整站停止由 BackoffRetryMiddleware 的 request_stop 处理，parse_pdf 只记这篇失败
    spider.parse_pdf(resp, article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert "Retry-After 3600" in (row["error"] or "")


# ---- P3：pdf_path 随源版本 / 下载失败失效 ----


def test_upsert_discovered_clears_pdf_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pdf = tmp_path / "alpha.pdf"
    pdf.write_bytes(b"%PDF")
    _fetched(conn, BASE, pdf_path=pdf.as_posix())
    store.upsert_discovered(
        conn, DiscoveredUrl(url=BASE, site="example", slug="alpha", lastmod="2026-02-01")
    )
    row = store.get_article(conn, BASE)
    assert row is not None and row["pdf_path"] is None


def test_process_response_fetched_clears_pdf_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pdf = tmp_path / "alpha.pdf"
    pdf.write_bytes(b"%PDF")
    _fetched(conn, BASE, pdf_path=pdf.as_posix())
    request = Request(BASE, cb_kwargs={"article_url": BASE})
    resp = HtmlResponse(BASE, status=200, body=HTML_WITH_PDF, request=request)
    process_response(conn, BASE, resp, tmp_path / "raw")
    row = store.get_article(conn, BASE)
    assert row is not None and row["pdf_path"] is None


def test_pdf_failure_clears_stale_pdf_path(conn: sqlite3.Connection, tmp_path: Path) -> None:
    pdf = tmp_path / "alpha.pdf"
    pdf.write_bytes(b"%PDF")
    _fetched(conn, BASE, pdf_path=pdf.as_posix())
    spider = _spider(conn, tmp_path)
    spider.parse_pdf(_pdf_response(PDF_URL, b"", status=503), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None and row["status"] == Status.FAILED
    assert row["pdf_path"] is None
    assert select_pdf_targets(conn, "example") == [BASE]


# ---- P5：补抓遵守 urls / limit，且不与 HTML 路径重复调度 ----


def test_select_pdf_targets_respects_urls_and_limit(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    urls = [f"https://www.example.test/papers/{i}" for i in range(3)]
    for i, url in enumerate(urls):
        store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug=f"a{i}"))
        store.set_status(conn, url, Status.FETCHED, raw_path=f"raw/example/a{i}.html")
    assert select_pdf_targets(conn, "example") == urls
    assert select_pdf_targets(conn, "example", urls=[urls[1]]) == [urls[1]]
    assert select_pdf_targets(conn, "example", limit=1) == [urls[0]]


def test_run_fetch_respects_urls_and_dedups_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scrapy.crawler

    db = tmp_path / "state.db"
    conn = store.connect(db)
    try:
        urls = [f"https://www.example.test/papers/{i}" for i in range(4)]
        for i, url in enumerate(urls):
            raw = tmp_path / f"a{i}.html"
            raw.write_text('<a href="/a.pdf">PDF</a>')
            store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug=f"a{i}"))
            store.set_status(conn, url, Status.FETCHED, raw_path=str(raw))
    finally:
        conn.close()

    seen: dict[str, object] = {}

    class _FakeSpider:
        stop_reason: str | None = None

    class _FakeCrawler:
        spider = _FakeSpider()

    class Crawler:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create_crawler(self, spidercls: object) -> _FakeCrawler:
            return _FakeCrawler()

        def crawl(self, crawler: object, **kwargs: object) -> None:
            seen.update(kwargs)

        def start(self) -> None:
            pass

    monkeypatch.setattr(scrapy.crawler, "CrawlerProcess", Crawler)
    site = SiteConfig(
        "example", "allowed", {"base_url": "https://www.example.test", "pdf": {"selector": "a"}}
    )
    run_fetch(site, db, tmp_path / "raw", urls=[urls[0]], limit=1)
    assert seen["urls"] == [urls[0]]
    assert seen["pdf_urls"] == []


# ---- P6：补抓只选"正文过短 / 缺 PDF"，不碰 HTML 拒绝错误 ----


def test_select_pdf_targets_skips_non_pdf_failure(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _fetched(conn, BASE)
    store.record_failure(conn, BASE, "HTTP 403: 拒绝访问", http_status=403)
    assert select_pdf_targets(conn, "example") == []


def test_select_pdf_targets_picks_pdf_failure(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _fetched(conn, BASE)
    store.record_failure(conn, BASE, "pdf: HTTP 404")
    assert select_pdf_targets(conn, "example") == [BASE]


# ---- P7：PDF 写盘异常要落失败状态 ----


def test_pdf_write_failure_records_failure(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qbkb.fetch.spider as spider_mod

    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    store.set_status(conn, BASE, Status.FETCHED, raw_path="raw/example/alpha.html")
    counts: Counter[str] = Counter()
    spider = _spider(conn, tmp_path, pdf_counts=counts)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("offline disk full")

    monkeypatch.setattr(spider_mod, "_write_atomic", fail)
    spider.parse_pdf(_pdf_response(PDF_URL, b"%PDF-1.7 data"), article_url=BASE)
    row = store.get_article(conn, BASE)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert (row["error"] or "").startswith("pdf: 落库失败")
    assert counts == Counter({"failed": 1})


# ---- P8：正文够长就不下载 PDF ----


LONG_HTML_WITH_PDF = (
    b"<html><body><article><p>" + b"word " * 400 + b"</p></article>"
    b'<aside class="sidebar"><ul class="file-list">'
    b'<a href="/wp-content/uploads/report.pdf">Download PDF</a></ul></aside></body></html>'
)


def test_long_body_does_not_schedule_pdf(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    spider = _spider(conn, tmp_path, extract_rules=ExtractRules(site="example"))
    request = Request(BASE, cb_kwargs={"article_url": BASE})
    resp = TextResponse(request.url, status=200, body=LONG_HTML_WITH_PDF, request=request)
    assert spider.parse_article(resp, article_url=BASE) is None


def test_short_body_schedules_pdf_with_rules(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    spider = _spider(conn, tmp_path, extract_rules=ExtractRules(site="example"))
    request = Request(BASE, cb_kwargs={"article_url": BASE})
    resp = TextResponse(request.url, status=200, body=HTML_WITH_PDF, request=request)
    scheduled = spider.parse_article(resp, article_url=BASE)
    assert isinstance(scheduled, Request)
    assert scheduled.url == PDF_URL


# ---- P9：HTML 跳转后相对 PDF 按最终 response.url 解析 ----


def test_relative_pdf_resolved_against_response_url(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=BASE, site="example", slug="alpha"))
    spider = _spider(conn, tmp_path)
    moved = "https://www.example.test/moved/article"
    body = (
        b'<html><body><aside class="sidebar"><ul class="file-list">'
        b'<a href="report.pdf">Download PDF</a></ul></aside></body></html>'
    )
    request = Request(moved, cb_kwargs={"article_url": BASE})
    resp = HtmlResponse(moved, status=200, body=body, request=request)
    scheduled = spider.parse_article(resp, article_url=BASE)
    assert isinstance(scheduled, Request)
    assert scheduled.url == "https://www.example.test/moved/report.pdf"


# ---- 第二轮：P1 严格同 host（子域也不行）+ P5 共享 --limit 预算 ----


def _strict_request(url: str, article_url: str = "http://example.test/a") -> Request:
    return Request(url, cb_kwargs={"article_url": article_url}, meta={META_STRICT_HOST: True})


class _StubSpider:
    def fail_cross_host(self, article_url: str, target_url: str) -> None:
        pass


def test_strict_host_middleware_rejects_subdomain() -> None:
    mw = HostAllowlistMiddleware("http", "example.test")
    req = _strict_request("http://child.example.test/outside.pdf")
    with pytest.raises(IgnoreRequest):
        mw.process_request(req, _StubSpider())  # type: ignore[arg-type]


def test_strict_host_middleware_allows_same_host() -> None:
    mw = HostAllowlistMiddleware("http", "example.test")
    mw.process_request(_strict_request("http://example.test/x.pdf"), _StubSpider())  # type: ignore[arg-type]


def test_default_middleware_still_allows_subdomain() -> None:
    # 不带严格标记的请求（HTML）保留原有允许子域策略
    mw = HostAllowlistMiddleware("http", "example.test")
    req = Request(
        "http://child.example.test/x.html", cb_kwargs={"article_url": "http://example.test/a"}
    )
    mw.process_request(req, _StubSpider())  # type: ignore[arg-type]


def test_pdf_subdomain_redirect_not_requested(tmp_path: Path) -> None:
    """真实通道：/a.pdf 302 到 child.example.test，子域的 PDF 正文必须没有被请求。"""
    helper = Path(__file__).with_name("crawl_pdf_local_helper.py")
    proc = subprocess.run(
        [sys.executable, str(helper), "subdomain", str(tmp_path)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-3000:]
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    paths = [h["path"] for h in result["hits"]]
    assert "/a.pdf" in paths  # 初始 PDF 请求确实发出
    assert "/outside.pdf" not in paths  # 子域的 PDF 正文没有被请求
    # 子域最多只被 robots 中间件探一次 robots.txt；非 robots 请求一个都不能落在子域
    non_robots = [h for h in result["hits"] if h["path"] != "/robots.txt"]
    assert all(not h["host"].startswith("child.example.test") for h in non_robots)
    row = result["rows"][0]
    assert row["status"] == "failed"
    assert row["pdf_path"] is None


def test_pdf_retry_after_stops_site(tmp_path: Path) -> None:
    """真实通道：/a.pdf 429 + Retry-After: 3600 → 整站停止，/b.pdf 没被请求。"""
    helper = Path(__file__).with_name("crawl_pdf_local_helper.py")
    proc = subprocess.run(
        [sys.executable, str(helper), "retry_after", str(tmp_path)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-3000:]
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    paths = [h["path"] for h in result["hits"]]
    assert "/a.pdf" in paths  # 第一篇 PDF 请求确实发出
    assert "/b.pdf" not in paths  # 整站已停，第二篇没被请求
    rows = {r["slug"]: r for r in result["rows"]}
    assert rows["a"]["status"] == "failed"
    assert "Retry-After 3600" in (rows["a"]["error"] or "")
    assert rows["b"]["status"] == "fetched"


def test_run_fetch_shares_limit_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scrapy.crawler

    db = tmp_path / "state.db"
    conn = store.connect(db)
    try:
        discovered = "https://www.example.test/papers/new"
        store.upsert_discovered(conn, DiscoveredUrl(url=discovered, site="example", slug="new"))
        fetched = "https://www.example.test/papers/old"
        raw = tmp_path / "old.html"
        raw.write_text('<a href="/a.pdf">PDF</a>')
        store.upsert_discovered(conn, DiscoveredUrl(url=fetched, site="example", slug="old"))
        store.set_status(conn, fetched, Status.FETCHED, raw_path=str(raw))
    finally:
        conn.close()

    seen: dict[str, object] = {}

    class _FakeSpider:
        stop_reason: str | None = None

    class _FakeCrawler:
        spider = _FakeSpider()

    class Crawler:
        def __init__(self, **kwargs: object) -> None:
            pass

        def create_crawler(self, spidercls: object) -> _FakeCrawler:
            return _FakeCrawler()

        def crawl(self, crawler: object, **kwargs: object) -> None:
            seen.update(kwargs)

        def start(self) -> None:
            pass

    monkeypatch.setattr(scrapy.crawler, "CrawlerProcess", Crawler)
    site = SiteConfig(
        "example", "allowed", {"base_url": "https://www.example.test", "pdf": {"selector": "a"}}
    )
    run_fetch(site, db, tmp_path / "raw", limit=1)
    assert seen["urls"] == [discovered]
    assert seen["pdf_urls"] == []


# ---- CFM：真实站点配置启用 PDF（侧栏 Download PDF + 正文 < 3000 排 PDF） ----


def test_cfm_pdf_config_reads_sidebar_selector_and_min_chars() -> None:
    site = config.load_site("cfm")
    rules = load_pdf_rules(site)
    assert rules is not None
    assert rules.selector == ".sidebar ul.file-list a"
    assert rules.min_chars == 3000


CFM_PAPER_HTML = (
    b'<html><body class="single-post">'
    b'<section class="content-blocks-container"><div class="row">'
    b'<div class="col-80">'
    b'<div class="content-block"><h1 class="appear">Toy Funds</h1></div>'
    b'<div class="content-block text-content appear">'
    b"<p><strong>Abstract</strong></p>"
    b"<p>Short abstract because the full paper is a PDF.</p>"
    b"</div></div>"
    b'<div class="col-20 sidebar">'
    b'<ul class="file-list">'
    b'<li class="appear"><a href="/wp-content/uploads/2020/09/Toy_Funds.pdf">Download PDF</a></li>'
    b"</ul>"
    b"</div></div></section>"
    b"</body></html>"
)


def test_cfm_short_body_schedules_sidebar_pdf(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """CFM 论文页：正文只有摘要（< 3000 字符）时，排侧栏 "Download PDF" 的 PDF。"""
    url = "https://www.cfm.com/toy-funds/"
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="cfm", slug="toy-funds"))
    site = config.load_site("cfm")
    spider = _spider(
        conn,
        tmp_path,
        pdf_rules=load_pdf_rules(site),
        extract_rules=load_rules(site),
    )
    request = Request(url, cb_kwargs={"article_url": url})
    resp = TextResponse(request.url, status=200, body=CFM_PAPER_HTML, request=request)
    scheduled = spider.parse_article(resp, article_url=url)
    assert isinstance(scheduled, Request)
    assert scheduled.url == "https://www.cfm.com/wp-content/uploads/2020/09/Toy_Funds.pdf"
