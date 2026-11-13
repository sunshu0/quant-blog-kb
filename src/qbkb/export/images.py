"""图片下载（design.md 4.6）：过合规闸门、只下本站图片、串行、间隔 ≥ 1 秒、带 UA。"""

from __future__ import annotations

import contextlib
import hashlib
import html
import os
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from qbkb.config import SiteConfig, ensure_allowed, is_local_host
from qbkb.fetch.settings import TIMEOUT_SECONDS, USER_AGENT, site_crawl_delay

MIN_INTERVAL_SECONDS = 1.0
MAX_IMAGE_BYTES = 10 * 1024 * 1024
_EXT_BY_TYPE = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
}
_KNOWN_EXT = frozenset({*_EXT_BY_TYPE.values(), ".jpeg"})


class ImageError(Exception):
    """这张图下载不了（原因写在消息里）。"""


def image_stem(url: str) -> str:
    """本地文件名（不含扩展名）：地址的 sha1 前 16 位。同一张图只存一份。"""
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


class ImageFetcher:
    """把图片下载到 images_dir，返回文件名。

    - 创建时过合规闸门（ensure_allowed）；
    - 只下载站点本身（base_url 的 host）和站点配置 export.image_hosts 里列出的 host，
      其他地址抛 ImageError（保留原链接）；
    - 已经下载过的直接返回，不发请求；同一次运行里失败过的不再重试；
    - 串行，任意两次请求之间至少间隔 min_interval 秒（真实站点不低于 1 秒）。
    """

    def __init__(
        self,
        site: SiteConfig,
        images_dir: Path,
        *,
        min_interval: float = MIN_INTERVAL_SECONDS,
        max_bytes: int = MAX_IMAGE_BYTES,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        ensure_allowed(site)
        base = site.raw.get("base_url")
        if not isinstance(base, str) or not base:
            raise ValueError(f"站点 {site.name}: 缺少 base_url")
        export: dict[str, object] = site.raw.get("export") or {}
        extra = export.get("image_hosts") or []
        if not isinstance(extra, list):
            raise ValueError(f"站点 {site.name}: export.image_hosts 必须是列表")
        self.hosts = frozenset({urlsplit(base).netloc.lower(), *(str(h).lower() for h in extra)})
        self.images_dir = images_dir
        # 真实站点不能低于 MIN_INTERVAL_SECONDS；本机测试站点（*.test、127.0.0.1……）不限
        local = all(is_local_host(h.split(":")[0]) for h in self.hosts)
        self.min_interval = min_interval if local else max(min_interval, MIN_INTERVAL_SECONDS)
        # 站点要求的最小间隔（crawl_delay）对图片同样适用
        self.min_interval = max(self.min_interval, site_crawl_delay(site.raw) or 0.0)
        self.max_bytes = max_bytes
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self._failed: dict[str, str] = {}
        # 已下载图片的 stem → 文件名。第一次用时扫一遍目录，之后不再 glob
        self._known: dict[str, str] | None = None
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            transport=transport,
        )
        self.requests = 0

    def close(self) -> None:
        self._client.close()

    def existing(self, url: str) -> str | None:
        if self._known is None:
            self._known = {}
            if self.images_dir.is_dir():
                for p in sorted(self.images_dir.iterdir()):
                    if p.is_file() and p.suffix not in (".tmp", ".tsv"):
                        self._known.setdefault(p.stem, p.name)
        return self._known.get(image_stem(url))

    def is_external(self, src: str) -> bool:
        """http(s) 图片，但不在允许下载的 host 里（例如 Flourish 的缩略图）：保留原链接、不下载。"""
        parts = urlsplit(html.unescape(src).strip())
        return parts.scheme in ("http", "https") and parts.netloc.lower() not in self.hosts

    def get(self, src: str) -> str:
        """src 是 HTML 里的地址（可能带 &amp;）。返回 images_dir 下的文件名。"""
        url = html.unescape(src).strip()
        done = self.existing(url)
        if done is not None:
            return done
        if url in self._failed:
            raise ImageError(self._failed[url])
        try:
            return self._download(url)
        except ImageError as e:
            self._failed[url] = str(e)
            raise

    def _wait(self) -> None:
        if self._last is not None:
            gap = self._clock() - self._last
            if gap < self.min_interval:
                self._sleep(self.min_interval - gap)
        self._last = self._clock()

    def _download(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ImageError(f"不是 http(s) 地址：{url[:80]}")
        if parts.netloc.lower() not in self.hosts:
            raise ImageError(f"外站图片不下载（{parts.netloc}）")
        self._wait()
        self.requests += 1
        try:
            with self._client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise ImageError(f"HTTP {resp.status_code}")
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    raise ImageError(f"内容类型 {ctype or '未知'} 不是图片")
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > self.max_bytes:
                        raise ImageError(f"超过 {self.max_bytes} 字节")
        except httpx.HTTPError as e:
            raise ImageError(f"{type(e).__name__}: {e}") from e
        suffix = PurePosixPath(parts.path).suffix.lower()
        ext = suffix if suffix in _KNOWN_EXT else _EXT_BY_TYPE.get(ctype, ".img")
        name = image_stem(url) + ext
        self.images_dir.mkdir(parents=True, exist_ok=True)
        path = self.images_dir / name
        tmp = path.with_name(name + ".tmp")
        try:
            tmp.write_bytes(bytes(buf))
            os.replace(tmp, path)
        except OSError as e:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise ImageError(f"写盘失败：{e}") from e
        if self._known is not None:
            self._known[image_stem(url)] = name
        return name
