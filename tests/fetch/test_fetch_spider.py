import asyncio
import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import pytest
import scrapy
from scrapy.http import HtmlResponse, Request
from scrapy.utils.test import get_crawler

from qbkb import config, store
from qbkb.config import ComplianceError, SiteConfig
from qbkb.fetch import USER_AGENT, Kind, build_settings, run_fetch, select_targets
from qbkb.fetch import spider as spider_mod
from qbkb.fetch.spider import ArticleSpider, BackoffRetryMiddleware, backoff_delay
from qbkb.models import DiscoveredUrl, Status

BASE = "https://www.example.test/insights/"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[tuple[Path, sqlite3.Connection]]:
    path = tmp_path / "state.db"
    c = store.connect(path)
    for slug in ("alpha", "beta", "report.pdf"):
        store.upsert_discovered(c, DiscoveredUrl(BASE + slug, "example", slug, "2026-01-01"))
    store.upsert_discovered(
        c, DiscoveredUrl("https://other.test/insights/x", "other", "x", "2026-01-01")
    )
    yield path, c
    c.close()


def test_settings_are_polite() -> None:
    s = build_settings()
    assert s["USER_AGENT"] == USER_AGENT == "qbkb/0.1 (+https://github.com/sunshu0/quant-blog-kb)"
    assert s["ROBOTSTXT_OBEY"] is True
    assert s["AUTOTHROTTLE_ENABLED"] is True
    assert s["CONCURRENT_REQUESTS_PER_DOMAIN"] == 1
    assert s["DOWNLOAD_DELAY"] == 2.0
    assert s["DOWNLOAD_TIMEOUT"] == 30
    assert s["RETRY_TIMES"] == 3
    assert {429, 500, 503} <= set(s["RETRY_HTTP_CODES"])
    assert 403 not in s["RETRY_HTTP_CODES"]
    assert 404 not in s["RETRY_HTTP_CODES"]
    assert s["HTTPCACHE_ENABLED"] is False
    assert s["REMOTE_CONTROL_ENABLED"] is False
    assert build_settings(http_cache=True)["HTTPCACHE_ENABLED"] is True


@pytest.mark.parametrize(
    ("attempt", "retry_after", "expected"),
    [
        (1, None, 2.0),
        (2, None, 4.0),
        (3, None, 8.0),
        (10, None, 60.0),  # 上限
        (1, "5", 5.0),
        (1, "999", 60.0),
        (2, "Wed, 21 Oct 2026 07:28:00 GMT", 4.0),  # HTTP 日期不解析，退回指数退避
    ],
)
def test_backoff_delay(attempt: int, retry_after: str | None, expected: float) -> None:
    assert backoff_delay(attempt, retry_after) == expected


class _Dummy(scrapy.Spider):
    name = "dummy"


def _middleware() -> BackoffRetryMiddleware:
    crawler = get_crawler(_Dummy, build_settings())
    crawler.spider = _Dummy.from_crawler(crawler)
    return BackoffRetryMiddleware.from_crawler(crawler)


def test_middleware_retries_three_times_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(spider_mod, "_sleep", fake_sleep)
    mw = _middleware()
    request = Request(BASE + "alpha")
    for _ in range(3):
        resp = HtmlResponse(request.url, status=503, body=b"busy", request=request)
        result = asyncio.run(mw.process_response(request, resp))
        assert isinstance(result, Request)
        request = result
    assert request.meta["retry_times"] == 3
    assert delays == [2.0, 4.0, 8.0]
    # 第 4 次失败：不再重试，原响应交给回调去 record_failure
    final = HtmlResponse(request.url, status=503, body=b"busy", request=request)
    assert asyncio.run(mw.process_response(request, final)) is final
    assert delays == [2.0, 4.0, 8.0]


def test_middleware_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(spider_mod, "_sleep", fake_sleep)
    request = Request(BASE + "alpha")
    resp = HtmlResponse(
        request.url, status=429, headers={"Retry-After": "7"}, body=b"", request=request
    )
    assert isinstance(asyncio.run(_middleware().process_response(request, resp)), Request)
    assert delays == [7.0]


def test_middleware_does_not_retry_403(monkeypatch: pytest.MonkeyPatch) -> None:
    request = Request(BASE + "alpha")
    resp = HtmlResponse(request.url, status=403, body=b"no", request=request)
    assert asyncio.run(_middleware().process_response(request, resp)) is resp


def test_select_targets_defaults_to_discovered_of_site(
    db: tuple[Path, sqlite3.Connection],
) -> None:
    path, conn = db
    store.set_status(conn, BASE + "beta", Status.FETCHED)
    assert select_targets(path, "example") == [BASE + "alpha", BASE + "report.pdf"]
    assert select_targets(path, "example", limit=1) == [BASE + "alpha"]
    # 指定 URL（每月复查）：只保留属于该站点、库里存在的
    urls = [BASE + "beta", "https://other.test/insights/x", BASE + "missing"]
    assert select_targets(path, "example", urls=urls) == [BASE + "beta"]


def test_spider_start_marks_pdf_and_sends_conditional_request(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    _, conn = db
    raw = tmp_path / "raw" / "example" / "beta.html"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"<html>old</html>")
    store.set_status(conn, BASE + "beta", Status.FETCHED, etag='"v1"', raw_path=raw.as_posix())
    counts: Counter[Kind] = Counter()
    spider = ArticleSpider(
        conn=conn,
        urls=[BASE + "alpha", BASE + "beta", BASE + "report.pdf"],
        raw_dir=tmp_path / "raw",
        allowed_host="www.example.test",
        counts=counts,
    )

    async def collect() -> list[Request]:
        return [r async for r in spider.start()]

    requests = asyncio.run(collect())
    assert [r.url for r in requests] == [BASE + "alpha", BASE + "beta"]
    assert requests[0].headers.get("If-None-Match") is None
    assert requests[1].headers.get("If-None-Match") == b'"v1"'
    assert requests[1].cb_kwargs == {"article_url": BASE + "beta"}
    assert counts[Kind.PDF] == 1
    row = store.get_article(conn, BASE + "report.pdf")
    assert row is not None
    assert row["status"] == Status.PDF_PENDING


def test_spider_callback_writes_state(db: tuple[Path, sqlite3.Connection], tmp_path: Path) -> None:
    _, conn = db
    counts: Counter[Kind] = Counter()
    spider = ArticleSpider(
        conn=conn, urls=[], raw_dir=tmp_path, allowed_host="www.example.test", counts=counts
    )
    request = Request(BASE + "alpha", cb_kwargs={"article_url": BASE + "alpha"})
    resp = HtmlResponse(request.url, status=200, body=b"<html>ok</html>", request=request)
    spider.parse_article(resp, article_url=BASE + "alpha")
    assert counts == Counter({Kind.FETCHED: 1})
    row = store.get_article(conn, BASE + "alpha")
    assert row is not None
    assert row["status"] == Status.FETCHED


@pytest.mark.parametrize("decision", ["permission_needed", "blocked"])
def test_run_fetch_checks_compliance_first(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path, decision: str
) -> None:
    path, _ = db
    site = SiteConfig("example", decision, {"base_url": "https://www.example.test"})
    with pytest.raises(ComplianceError):
        run_fetch(site, path, tmp_path / "raw")


def test_run_fetch_with_nothing_to_do_does_not_start_crawler(tmp_path: Path) -> None:
    site = config.load_site("man")
    stats = run_fetch(site, tmp_path / "empty.db", tmp_path / "raw")
    assert (stats.fetched, stats.failed, stats.pdf, stats.not_modified) == (0, 0, 0, 0)
