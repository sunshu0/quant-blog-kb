"""sitemap XML 解析（design.md 4.1）。不依赖 Scrapy，可以直接用本地文件测试。"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field

# lxml 没有自带类型存根（lxml-stubs 不在冻结的依赖里），pyright 看不到 etree
from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

# 关掉实体解析和网络访问，防 XXE；huge_tree 保持默认关闭，防超大文档。
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
_GZIP_MAGIC = b"\x1f\x8b"


class SitemapError(ValueError):
    """sitemap 不是合法的 XML，或者根元素既不是 urlset 也不是 sitemapindex。"""


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


def parse_sitemap(data: bytes) -> Sitemap:
    """解析 urlset 或 sitemapindex。.xml.gz（gzip 压缩的原始文件）自动解压。"""
    if data[:2] == _GZIP_MAGIC:
        data = gzip.decompress(data)
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
