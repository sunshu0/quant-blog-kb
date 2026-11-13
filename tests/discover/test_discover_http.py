"""http_get：用 httpx.MockTransport 离线测试重定向和大小上限。"""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from qbkb.discover import SitemapError, http_get
from qbkb.fetch.settings import USER_AGENT

HOSTS = frozenset({"www.example.test"})
URL = "https://www.example.test/sitemap.xml"
BODY = b"<urlset></urlset>"


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def test_sends_user_agent_and_returns_body() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, content=BODY)

    assert http_get(URL, HOSTS, transport=_transport(handler)) == BODY
    assert seen[0].headers["User-Agent"] == USER_AGENT


def test_same_host_redirect_is_followed() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/sitemap.xml":
            return httpx.Response(301, headers={"Location": "/sitemap_index.xml"})
        return httpx.Response(200, content=BODY)

    assert http_get(URL, HOSTS, transport=_transport(handler)) == BODY


@pytest.mark.parametrize(
    "location",
    ["https://evil.test/sitemap.xml", "//evil.test/x.xml", "ftp://www.example.test/x.xml"],
)
def test_cross_host_redirect_is_refused(location: str) -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(302, headers={"Location": location})

    with pytest.raises(SitemapError, match="外站"):
        http_get(URL, HOSTS, transport=_transport(handler))
    assert calls == [URL]  # 外站一次都没访问


def test_redirect_loop_is_bounded() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(str(req.url))
        return httpx.Response(302, headers={"Location": "/sitemap.xml"})

    with pytest.raises(SitemapError, match="重定向超过"):
        http_get(URL, HOSTS, transport=_transport(handler))
    assert len(calls) == 6


def test_redirect_without_location_fails() -> None:
    with pytest.raises(SitemapError, match="Location"):
        http_get(URL, HOSTS, transport=_transport(lambda _r: httpx.Response(302)))


def test_oversized_body_is_aborted() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    with pytest.raises(SitemapError, match="超过 1000 字节"):
        http_get(URL, HOSTS, max_bytes=1000, transport=_transport(handler))


def test_http_error_status_raises() -> None:
    with pytest.raises(httpx.HTTPStatusError):
        http_get(URL, HOSTS, transport=_transport(lambda _r: httpx.Response(404)))


def test_discover_site_uses_http_get_with_site_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qbkb.discover.run as run_mod
    from qbkb import store
    from qbkb.config import SiteConfig
    from qbkb.discover import discover_site

    seen: list[tuple[str, frozenset[str]]] = []

    def fake_get(url: str, allowed_hosts: frozenset[str], **_: object) -> bytes:
        seen.append((url, allowed_hosts))
        return BODY

    monkeypatch.setattr(run_mod, "http_get", fake_get)
    site = SiteConfig("x", "allowed", {"base_url": "https://www.example.test"})
    conn = store.connect(tmp_path / "s.db")
    discover_site(site, conn, sleep=lambda _s: None)
    conn.close()
    assert seen == [(URL, HOSTS)]


def test_redirect_obeys_site_delay() -> None:
    waits: list[float] = []
    events: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        events.append(request.url.path)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/end"})
        assert waits == [10.5]
        return httpx.Response(200, content=b"done")

    result = http_get(
        "https://example.test/start",
        frozenset({"example.test"}),
        transport=httpx.MockTransport(handler),
        delay=10.5,
        sleep=waits.append,
    )
    assert result == b"done"
    assert events == ["/start", "/end"]
