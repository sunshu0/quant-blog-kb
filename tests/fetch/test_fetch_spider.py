import asyncio
import sqlite3
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import scrapy
from scrapy.exceptions import CloseSpider
from scrapy.http import HtmlResponse, Request, Response
from scrapy.utils.test import get_crawler

from qbkb import config, store
from qbkb.config import ComplianceError, SiteConfig
from qbkb.fetch import USER_AGENT, Kind, build_settings, run_fetch, select_targets
from qbkb.fetch import spider as spider_mod
from qbkb.fetch.process import META_RETRY_AFTER_REFUSED, classify_response
from qbkb.fetch.spider import (
    ArticleSpider,
    BackoffRetryMiddleware,
    backoff_delay,
    parse_retry_after,
)
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
    assert s["QBKB_RETRY_AFTER_MAX"] == 300.0
    assert build_settings(DOWNLOAD_DELAY=0)["DOWNLOAD_DELAY"] == 0
    assert s["REMOTE_CONTROL_ENABLED"] is False
    assert build_settings(http_cache=True)["HTTPCACHE_ENABLED"] is True


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 2.0), (2, 4.0), (3, 8.0), (10, 60.0)],
)
def test_backoff_delay(attempt: int, expected: float) -> None:
    assert backoff_delay(attempt) == expected


def test_backoff_delay_custom_base() -> None:
    assert backoff_delay(3, base=0.5, max_delay=1.5) == 1.5


NOW = datetime(2026, 11, 11, 3, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("5", 5.0),
        (" 7 ", 7.0),
        ("Wed, 11 Nov 2026 03:00:30 GMT", 30.0),
        ("Wed, 11 Nov 2026 02:00:00 GMT", 0.0),  # 已经过去
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=NOW) == expected


class _Dummy(scrapy.Spider):
    name = "dummy"


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _middleware(clock: FakeClock) -> BackoffRetryMiddleware:
    crawler = get_crawler(_Dummy, build_settings())
    crawler.spider = _Dummy.from_crawler(crawler)
    mw = BackoffRetryMiddleware.from_crawler(crawler)
    return BackoffRetryMiddleware(
        mw._retry,
        base=mw.base,
        max_delay=mw.max_delay,
        retry_after_max=mw.retry_after_max,
        clock=clock,
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(spider_mod, "_sleep", fake_sleep)
    return recorded


def _response(request: Request, status: int, headers: dict[str, str] | None = None) -> Response:
    return HtmlResponse(request.url, status=status, headers=headers, body=b"x", request=request)


def test_backoff_pauses_every_request_not_just_the_retry(sleeps: list[float]) -> None:
    clock = FakeClock()
    mw = _middleware(clock)
    busy = Request(BASE + "busy")
    retry = asyncio.run(mw.process_response(busy, _response(busy, 503)))
    assert isinstance(retry, Request)
    assert sleeps == []  # 不在原地等
    assert mw.resume_at == 102.0
    # 队列里的下一篇（不是那条重试）也要等到 resume_at
    clock.t = 100.5
    asyncio.run(mw.process_request(Request(BASE + "other")))
    assert sleeps == [1.5]
    # 时间过了就不再等
    clock.t = 103.0
    asyncio.run(mw.process_request(Request(BASE + "third")))
    assert sleeps == [1.5]


def test_three_retries_back_off_exponentially_then_give_up(sleeps: list[float]) -> None:
    clock = FakeClock()
    mw = _middleware(clock)
    request = Request(BASE + "alpha")
    pauses: list[float] = []
    for _ in range(3):
        result = asyncio.run(mw.process_response(request, _response(request, 503)))
        assert isinstance(result, Request)
        pauses.append(mw.resume_at - clock.t)
        clock.t = mw.resume_at
        request = result
    assert request.meta["retry_times"] == 3
    assert pauses == [2.0, 4.0, 8.0]
    # 第 4 次失败：不再重试，原响应交给回调去 record_failure；不再推迟
    before = mw.resume_at
    final = _response(request, 503)
    assert asyncio.run(mw.process_response(request, final)) is final
    assert mw.resume_at == before


def test_retry_after_within_limit_is_honoured(sleeps: list[float]) -> None:
    clock = FakeClock()
    mw = _middleware(clock)
    request = Request(BASE + "alpha")
    result = asyncio.run(
        mw.process_response(request, _response(request, 429, {"Retry-After": "120"}))
    )
    assert isinstance(result, Request)
    assert mw.resume_at == 220.0  # 照服务器说的等 120 秒，不截到 60


def test_retry_after_over_limit_is_not_retried(sleeps: list[float]) -> None:
    clock = FakeClock()
    mw = _middleware(clock)
    request = Request(BASE + "alpha")
    resp = _response(request, 429, {"Retry-After": "3600"})
    assert asyncio.run(mw.process_response(request, resp)) is resp
    assert request.meta[META_RETRY_AFTER_REFUSED] == 3600.0
    assert mw.resume_at == 0.0
    assert classify_response(resp).reason is not None


def test_exception_retry_also_pauses(sleeps: list[float]) -> None:
    clock = FakeClock()
    mw = _middleware(clock)
    request = Request(BASE + "alpha")
    result = asyncio.run(mw.process_exception(request, TimeoutError("t")))
    assert isinstance(result, Request)
    assert mw.resume_at == 102.0


def test_middleware_does_not_retry_403(sleeps: list[float]) -> None:
    mw = _middleware(FakeClock())
    request = Request(BASE + "alpha")
    resp = _response(request, 403)
    assert asyncio.run(mw.process_response(request, resp)) is resp
    assert mw.resume_at == 0.0


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


def _spider(conn: sqlite3.Connection, raw_dir: Path, counts: Counter[Kind]) -> ArticleSpider:
    return ArticleSpider(
        conn=conn, urls=[], raw_dir=raw_dir, allowed_host="www.example.test", counts=counts
    )


def test_spider_stops_when_retry_after_is_too_long(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    _, conn = db
    counts: Counter[Kind] = Counter()
    request = Request(BASE + "alpha", meta={META_RETRY_AFTER_REFUSED: 3600.0})
    resp = HtmlResponse(request.url, status=429, body=b"slow", request=request)
    with pytest.raises(CloseSpider):
        _spider(conn, tmp_path, counts).parse_article(resp, article_url=BASE + "alpha")
    assert counts == Counter({Kind.FAILED: 1})
    row = store.get_article(conn, BASE + "alpha")
    assert row is not None
    assert row["status"] == Status.FAILED
    assert "Retry-After 3600" in row["error"]


def test_spider_counts_store_errors_as_failed(
    db: tuple[Path, sqlite3.Connection], tmp_path: Path
) -> None:
    _, conn = db
    blocker = tmp_path / "raw"
    blocker.write_text("not a dir")
    counts: Counter[Kind] = Counter()
    request = Request(BASE + "alpha")
    resp = HtmlResponse(request.url, status=200, body=b"<html>ok</html>", request=request)
    _spider(conn, blocker, counts).parse_article(resp, article_url=BASE + "alpha")
    assert counts == Counter({Kind.FAILED: 1})


def test_run_fetch_removes_stale_tmp_before_crawling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scrapy.crawler

    path = tmp_path / "state.db"
    c = store.connect(path)
    store.upsert_discovered(c, DiscoveredUrl("https://www.man.com/insights/a", "man", "a"))
    c.close()
    stale = tmp_path / "raw" / "man" / "a.html.tmp"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"half")

    class NoCrawl:
        def __init__(self, settings: object) -> None:
            pass

        def crawl(self, *args: object, **kwargs: object) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr(scrapy.crawler, "CrawlerProcess", NoCrawl)
    stats = run_fetch(config.load_site("man"), path, tmp_path / "raw")
    assert stats.stale_tmp_removed == 1
    assert not stale.exists()
