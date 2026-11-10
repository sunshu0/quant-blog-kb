import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse, Request, Response, TextResponse

from qbkb import store
from qbkb.fetch.outcome import Kind
from qbkb.fetch.process import (
    META_RETRY_AFTER_REFUSED,
    classify_error,
    classify_response,
    cleanup_stale_tmp,
    conditional_headers,
    detect_challenge,
    process_error,
    process_response,
    raw_file,
)
from qbkb.models import DiscoveredUrl, Status

URL = "https://www.example.test/insights/alpha"
ARTICLE = (
    b"<html><head><title>Constructed test article</title></head>"
    b"<body><article><p>Made-up paragraph for tests.</p></article>"
    b'<form class="newsletter"><div class="g-recaptcha"></div></form></body></html>'
)
CF_CHALLENGE = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b'<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    b"</body></html>"
)


def _resp(
    status: int = 200,
    body: bytes = ARTICLE,
    headers: dict[str, str] | None = None,
    url: str = URL,
    retry_times: int | None = None,
    cls: type[TextResponse] = HtmlResponse,
) -> Response:
    meta = {} if retry_times is None else {"retry_times": retry_times}
    hdrs = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
    return cls(url=url, status=status, body=body, headers=hdrs, request=Request(url, meta=meta))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(URL, "example", "alpha", "2026-01-01"))
    yield c
    c.close()


# ---- classify_response：纯函数 ----


def test_200_html_is_fetched_with_etag() -> None:
    out = classify_response(_resp(headers={"ETag": '"abc"'}))
    assert out.kind == Kind.FETCHED
    assert out.http_status == 200
    assert out.etag == '"abc"'


def test_200_without_etag() -> None:
    assert classify_response(_resp()).etag is None


def test_304_is_not_modified() -> None:
    assert classify_response(_resp(304, b"")).kind == Kind.NOT_MODIFIED


def test_403_fails_without_bypass() -> None:
    out = classify_response(_resp(403, b"<html><body>Forbidden</body></html>"))
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert "403" in out.reason
    assert "不绕过" in out.reason


@pytest.mark.parametrize(
    ("status", "body", "headers"),
    [
        (403, CF_CHALLENGE, {}),
        (503, b'<html><body><div class="h-captcha"></div></body></html>', {}),
        (429, b'<html><script src="https://geo.captcha-delivery.com/c.js"></script></html>', {}),
        (200, b"<html><body>wait</body></html>", {"cf-mitigated": "challenge"}),
        (200, CF_CHALLENGE, {}),  # 200 但页面很小、标题是验证页
    ],
)
def test_challenge_pages_fail(status: int, body: bytes, headers: dict[str, str]) -> None:
    out = classify_response(_resp(status, body, headers))
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert "人机验证" in out.reason
    assert "不绕过" in out.reason


def test_normal_article_with_recaptcha_form_is_not_a_challenge() -> None:
    assert detect_challenge(_resp()) is None


def test_large_200_page_with_suspicious_title_is_not_a_challenge() -> None:
    body = b"<html><head><title>Access Denied: a factor study</title></head><body>"
    body += b"<p>filler</p>" * 6000 + b"</body></html>"
    assert classify_response(_resp(body=body)).kind == Kind.FETCHED


@pytest.mark.parametrize("status", [404, 410])
def test_not_found_fails(status: int) -> None:
    out = classify_response(_resp(status, ARTICLE))  # 404 页带订阅表单不应被当成验证页
    assert out.kind == Kind.FAILED
    assert out.reason == f"HTTP {status}：页面不存在"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_status_reports_retry_count(status: int) -> None:
    out = classify_response(_resp(status, b"<html>busy</html>", retry_times=3))
    assert out.kind == Kind.FAILED
    assert out.reason == f"HTTP {status}：重试 3 次后仍失败"


def test_unexpected_status_fails() -> None:
    out = classify_response(_resp(300, b""))
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert "意外" in out.reason


@pytest.mark.parametrize(
    ("headers", "body", "url"),
    [
        ({"Content-Type": "application/pdf"}, b"%PDF-1.7 fake", URL),
        ({"Content-Type": "application/octet-stream"}, b"%PDF-1.4 fake", URL),
        ({}, b"<html>viewer</html>", "https://www.example.test/files/report.PDF"),
    ],
)
def test_pdf_is_pending(headers: dict[str, str], body: bytes, url: str) -> None:
    out = classify_response(_resp(body=body, headers=headers, url=url, cls=TextResponse))
    assert out.kind == Kind.PDF


def test_non_html_content_fails() -> None:
    out = classify_response(
        _resp(body=b"{}", headers={"Content-Type": "application/json"}, cls=TextResponse)
    )
    assert out.kind == Kind.FAILED
    assert out.reason == "内容类型 application/json 不是 HTML"


def test_empty_body_fails() -> None:
    out = classify_response(_resp(body=b"  "))
    assert out.kind == Kind.FAILED
    assert out.reason == "空响应"


def test_robots_block_is_reported() -> None:
    out = classify_error(IgnoreRequest("Forbidden by robots.txt"))
    assert out.reason == "robots.txt 禁止抓取"


def test_network_error_reports_retries() -> None:
    out = classify_error(TimeoutError("timed out"), retry_times=3)
    assert out.kind == Kind.FAILED
    assert out.reason == "请求失败（TimeoutError: timed out），重试 3 次后放弃"


# ---- process_response：落到状态库和 raw/ ----


def test_fetched_writes_raw_and_status(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    out = process_response(conn, URL, _resp(headers={"ETag": '"v1"'}), raw_dir)
    assert out.kind == Kind.FETCHED
    path = raw_dir / "example" / "alpha.html"
    assert path.read_bytes() == ARTICLE
    assert not list(path.parent.glob("*.tmp"))
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.FETCHED
    assert row["http_status"] == 200
    assert row["raw_path"] == path.resolve().as_posix()
    assert Path(row["raw_path"]).is_absolute()
    assert row["etag"] == '"v1"'
    assert row["error"] is None


def test_redirected_response_is_stored_under_original_url(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    resp = _resp(url="https://www.example.test/insights/alpha-renamed")
    process_response(conn, URL, resp, tmp_path)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.FETCHED
    assert (tmp_path / "example" / "alpha.html").is_file()


def test_304_leaves_state_untouched(conn: sqlite3.Connection, tmp_path: Path) -> None:
    process_response(conn, URL, _resp(headers={"ETag": '"v1"'}), tmp_path)
    store.set_status(conn, URL, Status.EXTRACTED)
    before = dict(store.get_article(conn, URL) or {})
    out = process_response(conn, URL, _resp(304, b""), tmp_path)
    assert out.kind == Kind.NOT_MODIFIED
    assert dict(store.get_article(conn, URL) or {}) == before
    assert (tmp_path / "example" / "alpha.html").read_bytes() == ARTICLE


def test_403_records_failure(conn: sqlite3.Connection, tmp_path: Path) -> None:
    process_response(conn, URL, _resp(403, CF_CHALLENGE), tmp_path)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert row["http_status"] == 403
    assert row["retries"] == 1
    assert "人机验证" in row["error"]
    assert not (tmp_path / "example").exists()  # 验证页不当原文存


def test_404_records_failure(conn: sqlite3.Connection, tmp_path: Path) -> None:
    process_response(conn, URL, _resp(404, b"<html>gone</html>"), tmp_path)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert row["error"] == "HTTP 404：页面不存在"


def test_failures_accumulate_retries(conn: sqlite3.Connection, tmp_path: Path) -> None:
    process_response(conn, URL, _resp(503, b"<html>busy</html>", retry_times=3), tmp_path)
    process_error(conn, URL, TimeoutError("timed out"), retry_times=3)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["retries"] == 2
    assert row["http_status"] is None  # 最后一次没拿到响应
    assert "重试 3 次后放弃" in row["error"]


def test_pdf_marks_pending(conn: sqlite3.Connection, tmp_path: Path) -> None:
    resp = _resp(body=b"%PDF-1.7", headers={"Content-Type": "application/pdf"}, cls=TextResponse)
    process_response(conn, URL, resp, tmp_path)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.PDF_PENDING
    assert row["raw_path"] is None


def test_unknown_url_raises(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        process_response(conn, "https://www.example.test/insights/nope", _resp(), tmp_path)


# ---- 条件请求与路径 ----


def test_conditional_header_needs_etag_and_raw_file(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    row = store.get_article(conn, URL)
    assert row is not None
    assert conditional_headers(row) == {}  # 还没抓过
    process_response(conn, URL, _resp(headers={"ETag": '"v1"'}), tmp_path)
    row = store.get_article(conn, URL)
    assert row is not None
    assert conditional_headers(row) == {"If-None-Match": '"v1"'}
    Path(row["raw_path"]).unlink()
    assert conditional_headers(row) == {}  # 原文丢了就完整重抓


def test_no_conditional_header_without_etag(conn: sqlite3.Connection, tmp_path: Path) -> None:
    process_response(conn, URL, _resp(), tmp_path)
    row = store.get_article(conn, URL)
    assert row is not None
    assert conditional_headers(row) == {}


@pytest.mark.parametrize("slug", ["", "..", ".hidden", "a/b", "a\\b"])
def test_raw_file_rejects_unsafe_slug(slug: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        raw_file(tmp_path, "example", slug)


# ---- Retry-After 超过上限 ----


def test_refused_retry_after_is_reported() -> None:
    url = URL
    resp = HtmlResponse(
        url,
        status=429,
        body=b"slow down",
        request=Request(url, meta={META_RETRY_AFTER_REFUSED: 3600.0}),
    )
    out = classify_response(resp)
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert "Retry-After 3600 秒" in out.reason
    assert "不重试" in out.reason


# ---- 落库出错时兜底 ----


def test_disk_error_counts_as_failed(conn: sqlite3.Connection, tmp_path: Path) -> None:
    blocker = tmp_path / "raw"
    blocker.write_text("不是目录")  # raw/<site>/ 建不出来 → OSError
    out = process_response(conn, URL, _resp(), blocker)
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert out.reason.startswith("落库失败（")
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.FAILED
    assert row["error"] == out.reason
    assert row["http_status"] == 200


def test_failed_replace_leaves_no_tmp(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qbkb.fetch.process as process_mod

    def boom(_src: object, _dst: object) -> None:
        raise PermissionError("文件被占用")

    monkeypatch.setattr(process_mod.os, "replace", boom)
    out = process_response(conn, URL, _resp(), tmp_path)
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert "PermissionError" in out.reason
    assert list((tmp_path / "example").iterdir()) == []


def test_sqlite_error_counts_as_failed(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qbkb.fetch.process as process_mod

    real = process_mod.store.set_status
    calls: list[str] = []

    def flaky(c: sqlite3.Connection, url: str, status: Status, **fields: object) -> None:
        calls.append(status)
        if status == Status.FETCHED:
            raise sqlite3.OperationalError("database is locked")
        real(c, url, status, **fields)  # type: ignore[arg-type]

    monkeypatch.setattr(process_mod.store, "set_status", flaky)
    out = process_response(conn, URL, _resp(), tmp_path)
    assert out.kind == Kind.FAILED
    assert out.reason is not None
    assert "OperationalError" in out.reason
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.FAILED


def test_store_completely_broken_still_returns_failed(tmp_path: Path) -> None:
    c = store.connect(tmp_path / "s.db")
    store.upsert_discovered(c, DiscoveredUrl(URL, "example", "alpha"))
    c.close()  # 之后任何 SQL 都抛 sqlite3.ProgrammingError，连兜底记录也写不进去
    for out in (
        process_error(c, URL, TimeoutError("t")),
        process_response(c, URL, _resp(), tmp_path),
    ):
        assert out.kind == Kind.FAILED
        assert out.reason is not None
        assert out.reason.startswith("落库失败")


def test_cleanup_stale_tmp(tmp_path: Path) -> None:
    site_dir = tmp_path / "example"
    site_dir.mkdir()
    (site_dir / "a.html.tmp").write_bytes(b"half")
    (site_dir / "b.html").write_bytes(b"keep")
    assert cleanup_stale_tmp(tmp_path, "example") == 1
    assert [p.name for p in site_dir.iterdir()] == ["b.html"]
    assert cleanup_stale_tmp(tmp_path, "missing-site") == 0
