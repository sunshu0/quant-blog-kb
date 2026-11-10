"""sitemap XML 解析（design.md 4.1）。不依赖 Scrapy，可以直接用本地文件测试。"""

from __future__ import annotations

import gzip
import io
import zlib
from dataclasses import dataclass, field

# lxml 没有自带类型存根（lxml-stubs 不在冻结的依赖里），pyright 看不到 etree
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

# 关掉实体解析和网络访问，防 XXE；huge_tree 保持默认关闭，防超大文档。
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
_GZIP_MAGIC = bytes((0x1F, 0x8B))
# 单份 sitemap 的上限（下载的字节、解压后的字节都算）。规范要求未压缩不超过 50 MB。
MAX_SITEMAP_BYTES = 50 * 1024 * 1024


class SitemapError(ValueError):
    """sitemap 无法使用：不是合法的 XML / gzip、根元素不对、超过大小上限、重定向到外站等。"""


@dataclass(frozen=True, slots=True)
class SitemapEntry:
    loc: str
    lastmod: str | None = None


@dataclass(frozen=True, slots=True)
class Sitemap:
    """一份 sitemap 的解析结果。urlset 只有 urls；sitemapindex 只有 sitemaps。"""

    urls: list[SitemapEntry] = field(default_factory=list)
    sitemaps: list[SitemapEntry] = field(default_factory=list)


def _local(tag: object) -> str:
    # 不同站点的命名空间写法不一（有的不写），只比较本地名。
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def _child_text(el: etree._Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name:
            text = (child.text or "").strip()
            return text or None
    return None


def _gunzip(data: bytes, max_bytes: int) -> bytes:
    """有上限地解压（防 gzip 炸弹）；损坏的 gzip 统一报 SitemapError。"""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
            out = f.read(max_bytes + 1)
    except (EOFError, OSError, zlib.error) as e:  # gzip.BadGzipFile 是 OSError 的子类
        raise SitemapError(f"gzip 已损坏: {e}") from e
    if len(out) > max_bytes:
        raise SitemapError(f"sitemap 解压后超过 {max_bytes} 字节")
    return out


def parse_sitemap(data: bytes, max_bytes: int = MAX_SITEMAP_BYTES) -> Sitemap:
    """解析 urlset 或 sitemapindex。.xml.gz（gzip 压缩的原始文件）有上限地自动解压。"""
    if len(data) > max_bytes:
        raise SitemapError(f"sitemap 超过 {max_bytes} 字节")
    if data[:2] == _GZIP_MAGIC:
        data = _gunzip(data, max_bytes)
    try:
        root = etree.fromstring(data, _PARSER)
    except etree.XMLSyntaxError as e:
        raise SitemapError(f"sitemap 不是合法的 XML: {e}") from e
    kind = _local(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise SitemapError(f"未知的 sitemap 根元素: {kind or root.tag!r}")
    child_name = "url" if kind == "urlset" else "sitemap"
    entries: list[SitemapEntry] = []
    for el in root:
        if _local(el.tag) != child_name:
            continue
        loc = _child_text(el, "loc")
        if loc:
            entries.append(SitemapEntry(loc=loc, lastmod=_child_text(el, "lastmod")))
    return Sitemap(urls=entries) if kind == "urlset" else Sitemap(sitemaps=entries)
