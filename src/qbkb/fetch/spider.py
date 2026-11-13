"""Scrapy 爬虫和重试退避中间件。以编程方式运行，不建 scrapy 项目目录。"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import scrapy
from scrapy.crawler import Crawler
from scrapy.downloadermiddlewares.retry import RetryMiddleware
from scrapy.exceptions import IgnoreRequest
from scrapy.http import Request, Response
from scrapy.utils.defer import deferred_from_coro
from twisted.python.failure import Failure

from qbkb import store
from qbkb.extract.rules import ExtractRules
from qbkb.fetch.outcome import Kind, Outcome
from qbkb.fetch.process import (
    META_RETRY_AFTER_REFUSED,
    _write_atomic,
    apply_outcome,
    conditional_headers,
    is_pdf_url,
    process_error,
    process_response,
    retry_after_refused_reason,
)
from qbkb.fetch.settings import (
    RETRY_AFTER_MAX_SECONDS,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
)
from qbkb.models import Status
from qbkb.pdf import PdfRules, find_pdf_url, pdf_raw_path, same_site


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
        self._crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        s = crawler.settings
        mw = cls(
            RetryMiddleware.from_crawler(crawler),
            base=s.getfloat("QBKB_BACKOFF_BASE", RETRY_BACKOFF_BASE_SECONDS),
            max_delay=s.getfloat("QBKB_BACKOFF_MAX", RETRY_BACKOFF_MAX_SECONDS),
            retry_after_max=s.getfloat("QBKB_RETRY_AFTER_MAX", RETRY_AFTER_MAX_SECONDS),
        )
        mw._crawler = crawler
        return mw

    def pause(self, seconds: float) -> None:
        self.resume_at = max(self.resume_at, self._clock() + seconds)

    def _stop_site(self, reason: str, request: Request, retry_after: float, status: int) -> None:
        """让整站停止：Retry-After 太久时，继续抓其他文章同样违背服务器要求。

        先无条件 fast 停站（合规停站不能依赖可失败的落库）；HTML 文章请求再把自己记成失败，
        记成功才标 _RETRY_AFTER_RECORDED 让 parse_article 跳过。PDF 请求的失败由 parse_pdf
        用 _pdf_fail 记（pdf: 前缀、pdf_counts），这里不重复记。robots 之类的内部请求
        没有文章可记，直接停。
        """
        spider = getattr(self._crawler, "spider", None) if self._crawler is not None else None
        stop = getattr(spider, "request_stop", None) if spider is not None else None
        if stop is not None:
            stop(reason)
        article_url = request.cb_kwargs.get("article_url")
        if (
            article_url is not None
            and spider is not None
            and not request.meta.get(META_STRICT_HOST)
        ):
            record = getattr(spider, "record_retry_after_failure", None)
            if record is not None and record(str(article_url), status, retry_after):
                request.meta[_RETRY_AFTER_RECORDED] = True

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
                self._stop_site(
                    f"服务器要求 Retry-After {retry_after:.0f} 秒，超过上限",
                    request,
                    retry_after,
                    response.status,
                )
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


# 白名单中间件已经记过一次失败后，放在 request.meta 里的标记；errback 看到它就跳过，不再记第二次。
_CROSS_HOST_BLOCKED = "qbkb_cross_host_blocked"
# 长 Retry-After 已经在 BackoffRetryMiddleware 里记过这一篇失败；parse_article 看到它就跳过。
_RETRY_AFTER_RECORDED = "qbkb_retry_after_recorded"
# PDF 请求在 meta 里带的标记：要求与文章严格同 scheme+host（子域也不行），由白名单在发出前校验
META_STRICT_HOST = "qbkb_strict_host"


class HostAllowlistMiddleware:
    """显式 scheme + host 白名单检查：约束初始 URL 和每一次重定向目标。

    Scrapy 自带的 OffsiteMiddleware 会被请求上的 dont_filter 直接放行（文章请求都带
    dont_filter=True），RedirectMiddleware 又把该标记继承给重定向请求，所以 allowed_domains
    实际拦不住跨站重定向。这里不理会 dont_filter，对进入下载器的每个请求（初始请求、HTTP 3xx
    和 meta refresh 生成的重定向请求）再做一次 scheme + host 检查：不匹配就把文章记成失败
    （写清原因），不保存任何内容。
    """

    def __init__(self, allowed_scheme: str, allowed_host: str) -> None:
        self.allowed_scheme = (allowed_scheme or "https").lower()
        self.allowed_host = (allowed_host or "").lower().rstrip(".")
        self._crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        spider = crawler.spider
        scheme = getattr(spider, "allowed_scheme", "https")
        host = getattr(spider, "allowed_domains", [""])[0]
        mw = cls(scheme, host)
        mw._crawler = crawler
        return mw

    def is_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if (parts.scheme or "").lower() != self.allowed_scheme:
            return False
        return self._host_allowed(url)

    def _host_allowed(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
        return host == self.allowed_host or host.endswith("." + self.allowed_host)

    def _request_allowed(self, request: Request) -> bool:
        """PDF 请求（带 META_STRICT_HOST）要求与文章严格同 scheme+host，子域也不行。

        重定向中间件把 meta 原样继承给重定向请求，所以链上每一跳在发出前都走这里校验。
        HTML 请求走原来的 is_allowed（允许同 host 子域）。
        """
        if request.meta.get(META_STRICT_HOST):
            article_url = request.cb_kwargs.get("article_url")
            return article_url is not None and same_site(str(article_url), request.url)
        return self.is_allowed(request.url)

    def process_request(self, request: Request) -> None:
        # Scrapy 2.14+ 不再给 process_request 传 spider 参数（新签名），spider 从 crawler 取。
        spider = getattr(self._crawler, "spider", None) if self._crawler is not None else None
        # 站点级停止（robots 跨站 / Retry-After 太久）需要 spider；拿不到 spider 时跳过，
        # 下面的白名单检查仍照常做，绝不「失败即放行」。
        stop_reason = getattr(spider, "stop_reason", None) if spider is not None else None
        if stop_reason is not None:
            # 整站已经被叫停：所有后续请求一律 IgnoreRequest，由 errback 处理但不记文章失败——
            # 这是站点级停止，不是这一篇的问题。
            raise IgnoreRequest(f"站点已停止抓取：{stop_reason}")
        # 白名单检查不依赖 spider：scheme + host 不匹配就拦，即使拿不到 spider 也不能放行。
        if self._request_allowed(request):
            return
        article_url = request.cb_kwargs.get("article_url")
        if article_url is None:
            # 内部请求（robots.txt 之类）：没有 article_url，不是文章，不能当文章记失败。
            # 同 host（scheme 可以不同，比如 http 站点把 robots 规范重定向到 https）放行；
            # 真跨 host 就停掉整站，不能放任 Scrapy 对 robots 失败默认放行、继续抓文章。
            if self._host_allowed(request.url):
                return
            if spider is not None:
                spider.request_stop(  # type: ignore[attr-defined]
                    f"robots.txt 被重定向到未许可站点 {request.url}，取不到 robots 规则"
                )
            raise IgnoreRequest(f"robots.txt 被重定向到未许可站点 {request.url}")
        if spider is not None:
            spider.fail_cross_host(str(article_url), request.url)  # type: ignore[attr-defined]
        # 已经记过一次失败：标记一下，errback 别再记第二次（否则失败数和 retries 都会翻倍）
        request.meta[_CROSS_HOST_BLOCKED] = True
        raise IgnoreRequest(f"请求目标 {request.url} 不在许可站点 {self.allowed_host} 内")


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
        *,
        allowed_scheme: str = "https",
        pdf_rules: PdfRules | None = None,
        pdf_urls: Sequence[str] = (),
        pdf_counts: Counter[str] | None = None,
        extract_rules: ExtractRules | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.conn = conn
        self.urls = list(urls)
        self.raw_dir = raw_dir
        self.allowed_domains = [allowed_host]
        self.allowed_scheme = allowed_scheme.lower()
        self.counts = counts
        # 整站停止的原因（robots 跨站、Retry-After 太久……）；设了之后不再发任何请求
        self.stop_reason: str | None = None
        self.pdf_rules = pdf_rules
        self.pdf_urls = list(pdf_urls)
        self.pdf_counts = pdf_counts if pdf_counts is not None else Counter()
        self.extract_rules = extract_rules

    def request_stop(self, reason: str) -> None:
        """请求停止整站抓取：记下原因，并 fast 关闭引擎（fire-and-forget）。

        robots 跨站被拦、服务器要求过长的 Retry-After 等"继续抓其他文章也违背服务器要求"的
        情况都会走这里。stop_reason 一记下，后续请求在 HostAllowlistMiddleware 里会被直接
        IgnoreRequest（不计文章失败）；引擎的 close_spider_async(fast) 会取消下载器里还没
        发出去的请求、并拒绝之后新进入下载器的请求。只调用一次（幂等）：第一次调用生效，
        之后再调用直接返回。
        """
        if self.stop_reason is not None:
            return
        self.stop_reason = reason
        engine = getattr(getattr(self, "crawler", None), "engine", None)
        if engine is not None:
            # Scrapy 2.20：close_spider_async 是 async，deferred_from_coro 把它调度到运行中的 loop。
            deferred_from_coro(engine.close_spider_async(reason=reason, mode="fast"))

    def fail_cross_host(self, article_url: str, target_url: str) -> None:
        """跨站（或换 scheme）请求：把文章记成失败，不保存目标内容。"""
        outcome = Outcome(Kind.FAILED, None, f"重定向到未许可站点 {target_url}，不保存内容")
        apply_outcome(self.conn, article_url, outcome, b"", self.raw_dir)
        self.counts[Kind.FAILED] += 1
        self.logger.warning("blocked cross-host request: %s -> %s", article_url, target_url)

    async def start(self) -> AsyncIterator[Request]:
        for url in self.urls:
            row = store.get_article(self.conn, url)
            if row is None:
                continue
            if is_pdf_url(url):
                # 直接指向 PDF 的网址：不做正文抽取，标记后留给按需处理
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
        if self.pdf_rules is None:
            return
        # 断点续跑：HTML 已经抓下来、但站内 PDF 还没下载（或文件丢了）的文章，只补 PDF。
        for url in self.pdf_urls:
            row = store.get_article(self.conn, url)
            if row is None or not row["raw_path"]:
                continue
            raw = Path(row["raw_path"])
            if not raw.is_file():
                continue
            raw_bytes = raw.read_bytes()
            pdf_url = find_pdf_url(raw_bytes, url, self.pdf_rules)
            if pdf_url is None:
                continue
            if not self._needs_pdf(raw_bytes, url):
                continue
            yield Request(
                pdf_url,
                callback=self.parse_pdf,
                errback=self.on_pdf_error,
                cb_kwargs={"article_url": url},
                meta={META_STRICT_HOST: True},
                dont_filter=True,
            )

    def record_retry_after_failure(self, article_url: str, status: int, retry_after: float) -> bool:
        """长 Retry-After：把当前文章记成失败，返回是否记成功。

        不经过 classify_response（那时 response.request 还没被引擎补上），直接用已知的
        status 和 Retry-After 秒数拼原因。落库失败只记日志、返回 False——不能因此阻止
        整站 fast 停站；失败计数留给 errback / parse_article 的兜底。
        """
        outcome = Outcome(Kind.FAILED, status, retry_after_refused_reason(status, retry_after))
        try:
            apply_outcome(self.conn, article_url, outcome, b"", self.raw_dir)
        except (OSError, sqlite3.Error, ValueError, KeyError) as e:
            self.logger.error("记录 Retry-After 失败时出错（整站停止已触发）：%s", e)
            return False
        self.counts[Kind.FAILED] += 1
        return True

    def parse_article(self, response: Response, article_url: str) -> Request | None:
        request = response.request
        if request is not None and request.meta.get(_RETRY_AFTER_RECORDED):
            return None  # 长 Retry-After 已经在 BackoffRetryMiddleware 里记过这一篇失败
        outcome = process_response(self.conn, article_url, response, self.raw_dir)
        self.counts[outcome.kind] += 1
        if outcome.kind == Kind.FAILED:
            self.logger.warning("failed %s: %s", article_url, outcome.reason)
        else:
            self.logger.info("%s %s %s", outcome.kind.value, outcome.http_status, article_url)
        if outcome.kind == Kind.FETCHED and self.pdf_rules is not None:
            pdf_url = find_pdf_url(
                response.body, response.url, self.pdf_rules, allowed_url=article_url
            )
            if pdf_url is not None and self._needs_pdf(response.body, article_url):
                return Request(
                    pdf_url,
                    callback=self.parse_pdf,
                    errback=self.on_pdf_error,
                    cb_kwargs={"article_url": article_url},
                    meta={META_STRICT_HOST: True},
                    dont_filter=True,
                )
        return None

    def _needs_pdf(self, body: bytes, article_url: str) -> bool:
        """网页正文（按抽取口径）短于 min_chars 才下载 PDF；没有抽取规则就保守地下。"""
        rules = self.extract_rules
        if rules is None or self.pdf_rules is None:
            return True
        from qbkb.extract.core import body_chars

        try:
            return body_chars(body, article_url, rules) < self.pdf_rules.min_chars
        except Exception:  # 抽不出正文或抽取本身出错：网页必然太短，交给 PDF 补
            return True

    def parse_pdf(self, response: Response, article_url: str) -> None:
        row = store.get_article(self.conn, article_url)
        if row is None or self.pdf_rules is None:
            return
        refused = response.meta.get(META_RETRY_AFTER_REFUSED)
        if refused is not None:
            # 服务器要求等很久：这篇记失败；整站停止已由 BackoffRetryMiddleware 的 request_stop
            # 处理（HostAllowlistMiddleware 会在后续请求发出前拦下它们）。
            self._pdf_fail(
                article_url,
                f"服务器要求 Retry-After {refused:.0f} 秒，超过上限，不重试并停止本次抓取",
            )
            return
        if not same_site(article_url, response.url):
            self._pdf_fail(article_url, f"PDF 跨站（最终响应 {response.url}）")
            return
        if response.status != 200:
            self._pdf_fail(article_url, f"HTTP {response.status}")
            return
        ctype = response.headers.get("Content-Type")
        ctype = ctype.decode("latin-1").split(";")[0].strip().lower() if ctype else ""
        body = response.body
        if (ctype and ctype != "application/pdf") or not body.startswith(b"%PDF"):
            self._pdf_fail(article_url, f"不是 PDF（内容类型 {ctype or '未知'}）")
            return
        if len(body) > self.pdf_rules.max_size_bytes:
            self._pdf_fail(article_url, f"PDF 超过大小上限 {self.pdf_rules.max_size_bytes} 字节")
            return
        try:
            path = pdf_raw_path(self.raw_dir.resolve(), str(row["site"]), str(row["slug"]))
            _write_atomic(path, body)
            # 成功回到 fetched（断点续跑时可能是 failed），清掉 pdf 错误，让 extract 接着处理
            store.set_status(
                self.conn, article_url, Status.FETCHED, pdf_path=path.as_posix(), error=None
            )
        except (OSError, sqlite3.Error, ValueError) as e:
            # 写盘/落库失败不能当成成功：记 pdf 失败，让 CLI 返回需处理
            self._pdf_fail(article_url, f"落库失败（{type(e).__name__}: {e}）")
            return
        self.pdf_counts["ok"] += 1
        self.logger.info("pdf %s %s", response.status, article_url)

    def _pdf_fail(self, article_url: str, reason: str) -> None:
        self.pdf_counts["failed"] += 1
        self.logger.warning("pdf failed %s: %s", article_url, reason)
        with contextlib.suppress(sqlite3.Error):
            store.record_failure(self.conn, article_url, f"pdf: {reason}")
            # 下载失败不再让旧 pdf_path 冒充"已下载"，保证下次还能被补抓选中
            store.set_fields(self.conn, article_url, pdf_path=None)

    def on_pdf_error(self, failure: Failure) -> None:
        request: Request = failure.request  # type: ignore[attr-defined]
        if request.meta.get(_CROSS_HOST_BLOCKED):
            return  # 白名单中间件已经记过失败，别重复计数 / 累加 retries
        if self.stop_reason is not None:
            return  # 整站停止（robots 跨站 / Retry-After），不是这一篇的失败，不记
        url = str(request.cb_kwargs["article_url"])
        exc = failure.value
        if not isinstance(exc, BaseException):
            exc = RuntimeError(str(exc))
        self._pdf_fail(url, f"{type(exc).__name__}: {exc}")

    def on_error(self, failure: Failure) -> None:
        request: Request = failure.request  # type: ignore[attr-defined]
        if request.meta.get(_CROSS_HOST_BLOCKED):
            return  # 白名单中间件已经记过失败，别重复计数 / 累加 retries
        if self.stop_reason is not None:
            return  # 整站停止（robots 跨站 / Retry-After），不是这一篇的失败，不记
        url = str(request.cb_kwargs["article_url"])
        exc = failure.value
        if not isinstance(exc, BaseException):
            exc = RuntimeError(str(exc))
        outcome = process_error(self.conn, url, exc, int(request.meta.get("retry_times", 0)))
        self.counts[outcome.kind] += 1
        self.logger.warning("failed %s: %s", url, outcome.reason)
