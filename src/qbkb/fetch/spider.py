"""Scrapy 爬虫和重试退避中间件。以编程方式运行，不建 scrapy 项目目录。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, Self

import scrapy
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.http import Request, Response
from twisted.python.failure import Failure

from qbkb import store
from qbkb.fetch.process import (
    Kind,
    Outcome,
    apply_outcome,
    conditional_headers,
    is_pdf_url,
    process_error,
    process_response,
)
from qbkb.fetch.settings import RETRY_BACKOFF_BASE_SECONDS, RETRY_BACKOFF_MAX_SECONDS


def backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """第 attempt 次重试（从 1 开始）前要等多少秒。

    Retry-After 是秒数时优先采用（HTTP 日期格式不解析，退回指数退避），都不超过上限。
    """
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after.strip()), RETRY_BACKOFF_MAX_SECONDS)
    delay = RETRY_BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0)
    return min(delay, RETRY_BACKOFF_MAX_SECONDS)


async def _sleep(seconds: float) -> None:
    """单独包一层，测试里替换它就不用真的等。"""
    await asyncio.sleep(seconds)


class BackoffRetryMiddleware:
    """在 Scrapy 自带的 RetryMiddleware 外面加指数退避。

    并发是 1，所以等待期间整个站点都暂停，这正是退避想要的效果。
    """

    def __init__(self, retry: RetryMiddleware) -> None:
        self._retry = retry

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        return cls(RetryMiddleware.from_crawler(crawler))

    @staticmethod
    async def _wait_before(retry: Request, retry_after: str | None) -> None:
        await _sleep(backoff_delay(int(retry.meta.get("retry_times", 1)), retry_after))

    async def process_response(self, request: Request, response: Response) -> Request | Response:
        result = self._retry.process_response(request, response)
        if isinstance(result, Request):
            value = response.headers.get("Retry-After")
            await self._wait_before(result, value.decode("latin-1") if value else None)
        return result

    async def process_exception(
        self, request: Request, exception: Exception
    ) -> Request | Response | None:
        result = self._retry.process_exception(request, exception)
        if isinstance(result, Request):
            await self._wait_before(result, None)
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
        self.logger.info("%s %s %s", outcome.kind.value, outcome.http_status, article_url)

    def on_error(self, failure: Failure) -> None:
        request: Request = failure.request  # type: ignore[attr-defined]
        url = str(request.cb_kwargs["article_url"])
        exc = failure.value
        if not isinstance(exc, BaseException):
            exc = RuntimeError(str(exc))
        outcome = process_error(self.conn, url, exc, int(request.meta.get("retry_times", 0)))
        self.counts[outcome.kind] += 1
        self.logger.warning("failed %s: %s", url, outcome.reason)
