"""图片下载：合规闸门、只下本站、间隔 ≥ 1 秒、UA、失败处理、已下载不重复请求。"""

from pathlib import Path

import httpx
import pytest

from qbkb.config import ComplianceError, SiteConfig
from qbkb.export import ImageError, ImageFetcher, image_stem
from qbkb.fetch.settings import USER_AGENT

SITE = SiteConfig(
    "e", "allowed", {"base_url": "https://e.test", "export": {"image_hosts": ["cdn.e.test"]}}
)
PNG = b"\x89PNG\r\n\x1a\nfake"


class Clock:
    def __init__(self) -> None:
        self.t = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _fetcher(
    tmp_path: Path, handler: object, clock: Clock, site: SiteConfig = SITE
) -> ImageFetcher:
    return ImageFetcher(
        site,
        tmp_path / "images",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
        sleep=clock.sleep,
        clock=clock,
    )


def test_downloads_serially_with_interval_and_ua(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=PNG)

    clock = Clock()
    f = _fetcher(tmp_path, handler, clock)
    a = f.get("https://e.test/a.png")
    clock.t += 0.3
    b = f.get("https://cdn.e.test/b?x=1&amp;y=2")  # HTML 转义的地址要先还原
    assert a == image_stem("https://e.test/a.png") + ".png"
    assert b == image_stem("https://cdn.e.test/b?x=1&y=2") + ".png"  # 扩展名按内容类型
    assert (tmp_path / "images" / a).read_bytes() == PNG
    assert clock.sleeps == [pytest.approx(0.7)]
    assert all(r.headers["User-Agent"] == USER_AGENT for r in seen)
    assert str(seen[1].url) == "https://cdn.e.test/b?x=1&y=2"

    # 已经下载过：不再请求
    assert f.get("https://e.test/a.png") == a
    assert len(seen) == 2 and f.requests == 2


def test_foreign_host_and_errors_are_not_downloaded(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if req.url.path == "/html":
            return httpx.Response(200, headers={"Content-Type": "text/html"}, content=b"<p>")
        return httpx.Response(404)

    f = _fetcher(tmp_path, handler, Clock())
    with pytest.raises(ImageError, match="外站"):
        f.get("https://other.test/x.png")
    with pytest.raises(ImageError, match="不是图片"):
        f.get("https://e.test/html")
    with pytest.raises(ImageError, match="404"):
        f.get("https://e.test/gone.png")
    with pytest.raises(ImageError, match="404"):
        f.get("https://e.test/gone.png")  # 同一次运行不重试
    assert calls["n"] == 2
    assert not (tmp_path / "images").exists()


def test_size_limit(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"x" * 50)

    f = ImageFetcher(
        SITE, tmp_path, max_bytes=10, transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    with pytest.raises(ImageError, match="超过"):
        f.get("https://e.test/big.png")


def test_site_crawl_delay_raises_interval(tmp_path: Path) -> None:
    """站点配置了 crawl_delay（robots.txt 的 Crawl-delay）时，图片之间也按它等。"""

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=PNG)

    slow = SiteConfig("e", "allowed", {"base_url": "https://e.test", "crawl_delay": 10.5})
    clock = Clock()
    f = _fetcher(tmp_path, handler, clock, slow)
    assert f.min_interval == 10.5
    f.get("https://e.test/a.png")
    clock.t += 0.5
    f.get("https://e.test/b.png")
    assert clock.sleeps == [pytest.approx(10.0)]


def test_compliance_gate(tmp_path: Path) -> None:
    blocked = SiteConfig("e", "permission_needed", {"base_url": "https://e.test"})
    with pytest.raises(ComplianceError):
        ImageFetcher(blocked, tmp_path)
