"""Scrapy 爬虫和重试退避中间件。以编程方式运行，不建 scrapy 项目目录。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self

import scrapy
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.exceptions import CloseSpider
from scrapy.http import Request, Response
from twisted.python.failure import Failure

from qbkb import store
from qbkb.fetch.outcome import Kind, Outcome
from qbkb.fetch.process import (
    META_RETRY_AFTER_REFUSED,
    apply_outcome,
    conditional_headers,
    is_pdf_url,
    process_error,
    process_response,
)
from qbkb.fetch.settings import (
    RETRY_AFTER_MAX_SECONDS,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
)


def backoff_delay(
    attempt: int,
    base: float = RETRY_BACKOFF_BASE_SECONDS,
    max_delay: float = RETRY_BACKOFF_MAX_SECONDS,
) -> float:
    """第 attempt 次重试（从 1 开始）前全站暂停多少秒：base * 2**(attempt-1)，不超过 max_delay。"""
    return min(base * 2 ** max(attempt - 1, 0), max_delay)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After 头 → 还要等多少秒。支持秒数和 HTTP 日期两种写法；看不懂返回 None。"""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        return float(v)
    try:
        when = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


async def _sleep(seconds: float) -> None:
    """单独包一层，测试里替换它就不用真的等。"""
    await asyncio.sleep(seconds)


class BackoffRetryMiddleware:
    """在 Scrapy 自带的 RetryMiddleware 外面加"全站"退避。

    需要重试时不在这个请求上原地等，而是把共享的"最早可继续时间"往后推；
    之后每个请求（不管是不是这条重试）在 process_request 里都要等到这个时间才发出。
    请求在进入中间件链之前就已计入下载器的并发，所以等待期间不会有别的请求发出去。

    Retry-After 不超过 retry_after_max 就照做；超过就不重试，在 request.meta 里记下秒数，
    由回调标失败并停止本次抓取（继续抓其他文章同样违背服务器的要求）。
    """

    def __init__(
        self,
        retry: RetryMiddleware,
        *,
        base: float = RETRY_BACKOFF_BASE_SECONDS,
        max_delay: float = RETRY_BACKOFF_MAX_SECONDS,
        retry_after_max: float = RETRY_AFTER_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retry = retry
        self.base = base
        self.max_delay = max_delay
        self.retry_after_max = retry_after_max
        self._clock = clock
        self.resume_at = 0.0

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        s = crawler.settings
        return cls(
            RetryMiddleware.from_crawler(crawler),
            base=s.getfloat("QBKB_BACKOFF_BASE", RETRY_BACKOFF_BASE_SECONDS),
            max_delay=s.getfloat("QBKB_BACKOFF_MAX", RETRY_BACKOFF_MAX_SECONDS),
            retry_after_max=s.getfloat("QBKB_RETRY_AFTER_MAX", RETRY_AFTER_MAX_SECONDS),
        )

    def pause(self, seconds: float) -> None:
        self.resume_at = max(self.resume_at, self._clock() + seconds)

    async def process_request(self, request: Request) -> None:
        wait = self.resume_at - self._clock()
        if wait > 0:
            await _sleep(wait)

    async def process_response(self, request: Request, response: Response) -> Request | Response:
        retry_after: float | None = None
        if response.status in self._retry.retry_http_codes and not request.meta.get("dont_retry"):
            header = response.headers.get("Retry-After")
            retry_after = parse_retry_after(header.decode("latin-1") if header else None)
            if retry_after is not None and retry_after > self.retry_after_max:
                request.meta[META_RETRY_AFTER_REFUSED] = retry_after
                return response
        result = self._retry.process_response(request, response)
        if isinstance(result, Request):
            attempt = int(result.meta.get("retry_times", 1))
            self.pause(
                retry_after
                if retry_after is not None
                else backoff_delay(attempt, self.base, self.max_delay)
            )
        return result

    async def process_exception(
        self, request: Request, exception: Exception
    ) -> Request | Response | None:
        result = self._retry.process_exception(request, exception)
        if isinstance(result, Request):
            attempt = int(result.meta.get("retry_times", 1))
            self.pause(backoff_delay(attempt, self.base, self.max_delay))
        return result


class ArticleSpider(scrapy.Spider):
    """抓给定的文章 URL，每个响应交给 process_response 落库。"""

    name = "qbkb_fetch"

    def __init__(
        self,
        conn: sqlite3.Connection,
        urls: Sequence[str],
        raw_dir: Path,
        allowed_host: str,
        counts: Counter[Kind],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.conn = conn
        self.urls = list(urls)
        self.raw_dir = raw_dir
        self.allowed_domains = [allowed_host]
        self.counts = counts

    async def start(self) -> AsyncIterator[Request]:
        for url in self.urls:
            row = store.get_article(self.conn, url)
            if row is None:
                continue
            if is_pdf_url(url):
                # 不下载 PDF，直接标记，留给以后的 PDF 流程
                apply_outcome(self.conn, url, Outcome(Kind.PDF, None), b"", self.raw_dir)
                self.counts[Kind.PDF] += 1
                continue
            yield Request(
                url,
                headers=conditional_headers(row),
                callback=self.parse_article,
                errback=self.on_error,
                cb_kwargs={"article_url": url},
                dont_filter=True,
            )

    def parse_article(self, response: Response, article_url: str) -> None:
        outcome = process_response(self.conn, article_url, response, self.raw_dir)
        self.counts[outcome.kind] += 1
        if outcome.kind == Kind.FAILED:
            self.logger.warning("failed %s: %s", article_url, outcome.reason)
        else:
            self.logger.info("%s %s %s", outcome.kind.value, outcome.http_status, article_url)
        if response.meta.get(META_RETRY_AFTER_REFUSED) is not None:
            # 服务器要求等很久：不只是这篇不重试，整个站点这次都不再请求
            raise CloseSpider("retry_after_too_long")

    def on_error(self, failure: Failure) -> None:
        request: Request = failure.request  # type: ignore[attr-defined]
        url = str(request.cb_kwargs["article_url"])
        exc = failure.value
        if not isinstance(exc, BaseException):
            exc = RuntimeError(str(exc))
        outcome = process_error(self.conn, url, exc, int(request.meta.get("retry_times", 0)))
        self.counts[outcome.kind] += 1
        self.logger.warning("failed %s: %s", url, outcome.reason)
