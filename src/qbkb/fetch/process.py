"""响应 → 状态库 的处理（design.md 4.2）。

不依赖正在运行的爬虫：测试里用 scrapy.http.HtmlResponse 构造响应就能直接调用。
classify_response 是纯函数，只看响应；process_response 再把结果落到状态库和 raw/。
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from scrapy.exceptions import IgnoreRequest
from scrapy.http import Response

from qbkb import store
from qbkb.fetch.settings import RETRY_HTTP_CODES
from qbkb.models import FetchResult, Status


class Kind(StrEnum):
    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"
    PDF = "pdf"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: Kind
    http_status: int | None
    reason: str | None = None
    etag: str | None = None


# 人机验证/反爬页面的特征。200 页面只认强特征（cf-mitigated 头、小页面的标题）：
# 普通文章页也可能带 reCAPTCHA 订阅表单或 Cloudflare 注入的 challenge-platform 脚本。
_CHALLENGE_TITLES = (
    "just a moment",
    "attention required",
    "verify you are human",
    "are you a robot",
    "security check",
    "access denied",
)
_CHALLENGE_BODY_MARKERS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
    "cf-chl-",
    "g-recaptcha",
    "h-captcha",
    "px-captcha",
    "_incapsula_resource",
    "captcha-delivery.com",
)
_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SNIFF_BYTES = 64 * 1024
_SMALL_PAGE_BYTES = 50 * 1024
# 只有这些状态码才检查页面里的验证码脚本特征（404 页也可能带订阅表单的 reCAPTCHA）
_BLOCKING_STATUSES = (401, 403, 429, 503)
_HTML_TYPES = ("text/html", "application/xhtml+xml")


def _header(response: Response, name: str) -> str | None:
    value = response.headers.get(name)
    return None if value is None else value.decode("latin-1").strip()


def _retry_times(response: Response) -> int:
    request = response.request
    return int(request.meta.get("retry_times", 0)) if request is not None else 0


def is_pdf_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def detect_challenge(response: Response) -> str | None:
    """疑似人机验证页面时返回命中的特征，否则 None。"""
    if (_header(response, "cf-mitigated") or "").lower() == "challenge":
        return "cf-mitigated: challenge"
    status, body = response.status, response.body
    head = body[:_SNIFF_BYTES]
    m = _TITLE.search(head)
    # 正常文章的标题也可能碰巧含这些词；200 时只在页面很小（验证页的典型大小）时才算数。
    if m and (status != 200 or len(body) < _SMALL_PAGE_BYTES):
        title = m.group(1).decode("utf-8", "replace").strip().lower()
        for marker in _CHALLENGE_TITLES:
            if marker in title:
                return f"标题含 {marker!r}"
    if status not in _BLOCKING_STATUSES:
        return None
    text = head.decode("utf-8", "replace").lower()
    for marker in _CHALLENGE_BODY_MARKERS:
        if marker in text:
            return f"页面含 {marker!r}"
    return None


def _content_type(response: Response) -> str:
    return (_header(response, "Content-Type") or "").split(";")[0].strip().lower()


def classify_response(response: Response) -> Outcome:
    """只看响应决定结果，不碰状态库和文件系统。"""
    status = response.status
    if status == 304:
        return Outcome(Kind.NOT_MODIFIED, status)
    challenge = detect_challenge(response)
    if challenge:
        return Outcome(
            Kind.FAILED, status, f"HTTP {status}：疑似人机验证页面（{challenge}），不绕过"
        )
    if status in (401, 403):
        return Outcome(Kind.FAILED, status, f"HTTP {status}：拒绝访问，不绕过")
    if status in (404, 410):
        return Outcome(Kind.FAILED, status, f"HTTP {status}：页面不存在")
    if status in RETRY_HTTP_CODES:
        n = _retry_times(response)
        return Outcome(Kind.FAILED, status, f"HTTP {status}：重试 {n} 次后仍失败")
    if not 200 <= status < 300:
        return Outcome(Kind.FAILED, status, f"HTTP {status}：意外的状态码")
    ctype = _content_type(response)
    body = response.body
    if ctype == "application/pdf" or body.startswith(b"%PDF") or is_pdf_url(response.url):
        return Outcome(Kind.PDF, status)
    if ctype and ctype not in _HTML_TYPES:
        return Outcome(Kind.FAILED, status, f"内容类型 {ctype} 不是 HTML")
    if not body.strip():
        return Outcome(Kind.FAILED, status, "空响应")
    return Outcome(Kind.FETCHED, status, etag=_header(response, "ETag"))


def classify_error(exc: BaseException, retry_times: int = 0) -> Outcome:
    """请求根本没拿到响应（robots 禁止、超时、DNS……）。"""
    if isinstance(exc, IgnoreRequest) and "robots.txt" in str(exc).lower():
        return Outcome(Kind.FAILED, None, "robots.txt 禁止抓取")
    detail = f"{type(exc).__name__}: {exc}".rstrip(": ")
    suffix = f"，重试 {retry_times} 次后放弃" if retry_times else ""
    return Outcome(Kind.FAILED, None, f"请求失败（{detail}）{suffix}")


def raw_file(raw_dir: Path, site: str, slug: str) -> Path:
    """raw/<site>/<slug>.html。slug 来自状态库，这里再挡一次路径穿越。"""
    for part in (site, slug):
        bad = not part or part.startswith(".") or "/" in part or "\\" in part
        if bad or part != Path(part).name:
            raise ValueError(f"不安全的路径片段: {part!r}")
    return raw_dir / site / f"{slug}.html"


def conditional_headers(row: sqlite3.Row) -> dict[str, str]:
    """有 etag 且本地原文还在时才发 If-None-Match；原文丢了就完整重抓，304 会让我们无内容可用。"""
    etag, raw_path = row["etag"], row["raw_path"]
    if etag and raw_path and Path(raw_path).is_file():
        return {"If-None-Match": str(etag)}
    return {}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def apply_outcome(
    conn: sqlite3.Connection, url: str, outcome: Outcome, body: bytes, raw_dir: Path
) -> FetchResult:
    """把结果落到状态库（和 raw/）。304 不改任何东西。"""
    row = store.get_article(conn, url)
    if row is None:
        raise KeyError(url)
    match outcome.kind:
        case Kind.NOT_MODIFIED:
            return FetchResult(url, outcome.http_status or 304, row["raw_path"], row["etag"], True)
        case Kind.FETCHED:
            path = raw_file(raw_dir, str(row["site"]), str(row["slug"]))
            _write_atomic(path, body)
            store.set_status(
                conn,
                url,
                Status.FETCHED,
                http_status=outcome.http_status,
                raw_path=path.as_posix(),
                etag=outcome.etag,
                error=None,
            )
            return FetchResult(url, outcome.http_status or 200, path.as_posix(), outcome.etag)
        case Kind.PDF:
            store.set_status(
                conn, url, Status.PDF_PENDING, http_status=outcome.http_status, error=None
            )
            return FetchResult(url, outcome.http_status or 0, None)
        case Kind.FAILED:
            store.set_status(conn, url, Status.FAILED, http_status=outcome.http_status)
            store.record_failure(conn, url, outcome.reason or "未知错误")
            return FetchResult(url, outcome.http_status or 0, None)


def process_response(
    conn: sqlite3.Connection, url: str, response: Response, raw_dir: Path
) -> Outcome:
    """爬虫回调用：分类 + 落库。url 是状态库里的原始 URL（重定向后 response.url 可能不同）。"""
    outcome = classify_response(response)
    apply_outcome(conn, url, outcome, response.body, raw_dir)
    return outcome


def process_error(
    conn: sqlite3.Connection, url: str, exc: BaseException, retry_times: int = 0
) -> Outcome:
    outcome = classify_error(exc, retry_times)
    apply_outcome(conn, url, outcome, b"", raw_dir=Path())
    return outcome
