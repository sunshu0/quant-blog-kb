"""站点配置里的 PDF 正文支持（design.md 4.2/4.3 的 PDF 部分）。

fetch 阶段用这里的规则判断要不要下载 PDF、下载哪个 PDF（`find_pdf_url`、`pdf_raw_path`）；
extract 阶段读 `pdf_path` 决定要不要用 PDF 正文（`load_pdf_rules` 的 `min_chars`）。

本模块不导入 Scrapy 也不导入 pypdf：discover、extract 等只用常量 / 配置的地方不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from cssselect import SelectorError
from lxml.cssselect import CSSSelector
from lxml.html import document_fromstring

from qbkb.config import SiteConfig

# 网页正文（抽取后）短于这个字符数就用 PDF 正文，除非站点配置里显式改掉
DEFAULT_MIN_CHARS = 500
# PDF 大小上限默认值（MB）
DEFAULT_MAX_SIZE_MB = 20.0


@dataclass(frozen=True, slots=True)
class PdfRules:
    """一个站点的 PDF 正文规则。只有配置了 `pdf:` 节的站点才启用，其余站点行为不变。"""

    selector: str
    min_chars: int = DEFAULT_MIN_CHARS
    max_size_bytes: int = int(DEFAULT_MAX_SIZE_MB * 1024 * 1024)


def load_pdf_rules(site: SiteConfig) -> PdfRules | None:
    """解析站点配置的 `pdf:` 节。没配置返回 None（该站点行为完全不变）。"""
    raw = site.raw.get("pdf")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"站点 {site.name}: pdf 节必须是映射")
    selector = raw.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError(f"站点 {site.name}: pdf.selector 必须是非空的 CSS 选择器字符串")
    try:
        CSSSelector(selector)
    except SelectorError as e:
        raise ValueError(f"站点 {site.name}: pdf.selector 无效：{selector!r}（{e}）") from e
    min_chars = raw.get("min_chars", DEFAULT_MIN_CHARS)
    if isinstance(min_chars, bool) or not isinstance(min_chars, int) or min_chars < 0:
        raise ValueError(f"站点 {site.name}: pdf.min_chars 必须是非负整数")
    max_size_mb = raw.get("max_size_mb", DEFAULT_MAX_SIZE_MB)
    if (
        isinstance(max_size_mb, bool)
        or not isinstance(max_size_mb, (int, float))
        or max_size_mb <= 0
    ):
        raise ValueError(f"站点 {site.name}: pdf.max_size_mb 必须是正数")
    return PdfRules(
        selector=selector,
        min_chars=min_chars,
        max_size_bytes=int(max_size_mb * 1024 * 1024),
    )


def same_site(article_url: str, target_url: str) -> bool:
    """两个 URL 是否同一站点（scheme + host 一致）。"""
    a = urlsplit(article_url)
    t = urlsplit(target_url)
    return (a.scheme, a.hostname) == (t.scheme, t.hostname)


def find_pdf_url(
    html: bytes, base_url: str, rules: PdfRules, *, allowed_url: str | None = None
) -> str | None:
    """在文章 HTML 里找 PDF 链接，返回同一站点的绝对地址；没有、解析失败或跨站返回 None。

    相对地址按 `base_url`（通常是最终 response.url）解析；是否跨站按 `allowed_url`（缺省就是
    `base_url`，也就是最初已许可的文章 URL）判定。两者可以不同：页面被同站重定向到别的目录时，
    相对 PDF 地址要按最终目录解析，但站点边界仍按最初许可的文章判断。
    只认 selector 命中的元素（通常是 `<a>`）；元素本身没有 href 时，在其后代里找一个 `<a>`。
    """
    reference = allowed_url if allowed_url is not None else base_url
    try:
        doc = document_fromstring(html.decode("utf-8", "replace"))
    except Exception:
        return None
    for el in doc.cssselect(rules.selector):
        href = el.get("href")
        if not href and el.tag != "a":
            for child in el.iterdescendants("a"):
                href = child.get("href")
                if href:
                    break
        if not href:
            continue
        absolute = urljoin(base_url, href.strip())
        if same_site(reference, absolute):
            return absolute
    return None


def pdf_raw_path(raw_dir: Path, site: str, slug: str) -> Path:
    """`raw/<site>/<slug>.pdf`。和 raw_file 一样再挡一次路径穿越。"""
    for part in (site, slug):
        bad = not part or part.startswith(".") or "/" in part or "\\" in part
        if bad or part != Path(part).name:
            raise ValueError(f"不安全的路径片段: {part!r}")
    return raw_dir / site / f"{slug}.pdf"
