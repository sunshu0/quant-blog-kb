"""清洗正文 HTML 并切成段落。

清洗后的 HTML 只保留"内容"：结构标签（标题、列表、表格、图片、脚注、公式）和少数有意义的
属性。布局用的 div / span 和 class、style 全部去掉，这样同一段文字在不同页面上得到同一个
hash，翻译记忆才能复用；也让 DeepL 的 HTML 模式少处理无关标签。
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable, Sequence
from urllib.parse import urljoin

import lxml.etree as etree
from lxml.html import HtmlElement, fragment_fromstring

from qbkb.models import Segment, normalize_html

# 拆掉标签、保留内容的布局容器。块级容器拆开前先把里面的零散文字包成 <p>，
# 否则 <div>甲</div><div>乙</div> 拆开后会粘成一段"甲乙"。
BLOCK_CONTAINERS = frozenset(
    {"div", "section", "article", "main", "header", "footer", "aside", "center", "body"}
)
INLINE_WRAPPERS = frozenset({"span", "font", "noscript", "picture", "source"})
# 切段单位：顶层的这些元素各自成为一个 Segment
BLOCK_TAGS = frozenset(
    {
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "dl",
        "table",
        "figure",
        "blockquote",
        "pre",
        "img",
        "math",
    }
)
# 没有文字也有意义的元素（<hr> 不在其中：分隔线对翻译和 Markdown 都没用，当空元素删掉）
SELF_MEANINGFUL = frozenset({"img", "br", "math", "td", "th", "tr"})
# 每个标签允许保留的属性；id 另外处理（只保留被页内链接引用的，比如脚注锚点）
ALLOWED_ATTRS: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"src", "alt", "title", "width", "height"}),
    "td": frozenset({"colspan", "rowspan"}),
    "th": frozenset({"colspan", "rowspan", "scope"}),
    "ol": frozenset({"start", "type", "reversed"}),
    "li": frozenset({"value"}),
    "abbr": frozenset({"title"}),
    "blockquote": frozenset({"cite"}),
    "q": frozenset({"cite"}),
}
MATHML_NS_TAGS = frozenset({"math"})
_PLACEHOLDER_SRC = ("data:", "about:blank")


def _is_mathml(el: HtmlElement) -> bool:
    """MathML 公式整棵子树原样保留（属性决定公式的含义，不能乱删）。"""
    for anc in el.iterancestors():
        if anc.tag in MATHML_NS_TAGS:
            return True
    return el.tag in MATHML_NS_TAGS


def _drop(el: HtmlElement) -> None:
    """删除元素但保留它后面的文字（tail）。"""
    el.drop_tree()


def remove_noise(root: HtmlElement, selectors: Iterable[str]) -> None:
    for sel in selectors:
        for el in root.cssselect(sel):
            if el is not root and el.getparent() is not None:
                _drop(el)


def _noscript_images(root: HtmlElement) -> None:
    """<noscript><img></noscript> 是图表嵌入的静态缩略图：留下图片，其余丢掉。"""
    for ns in list(root.iter("noscript")):
        parent = ns.getparent()
        imgs = [copy.deepcopy(i) for i in ns.iter("img")]
        if parent is None or not imgs:
            _drop(ns)
            continue
        idx = parent.index(ns)
        tail, ns.tail = ns.tail, None
        parent.remove(ns)
        for offset, img in enumerate(imgs):
            img.tail = None
            parent.insert(idx + offset, img)
        imgs[-1].tail = tail


def _lazy_images(root: HtmlElement) -> None:
    for img in list(root.iter("img")):
        src = img.get("src") or ""
        lazy = img.get("data-src") or img.get("data-lazy-src") or img.get("data-original")
        if lazy and (not src or src.startswith(_PLACEHOLDER_SRC)):
            img.set("src", lazy)
        if not img.get("src"):
            _drop(img)


def _links(root: HtmlElement, base_url: str) -> None:
    for a in list(root.iter("a")):
        href = (a.get("href") or "").strip()
        if not href or href == "#" or href.lower().startswith("javascript:"):
            a.drop_tag()  # 分享按钮之类：没有去处的链接只留文字
            continue
        if not href.startswith("#"):
            a.set("href", urljoin(base_url, href))
    for img in root.iter("img"):
        img.set("src", urljoin(base_url, (img.get("src") or "").strip()))


def _unwrap(root: HtmlElement) -> None:
    # <source> 只拆标签不删子树：lxml 的 HTML 解析器不认识 <picture>，
    # 会把后面的 <img> 当成 <source> 的子元素
    for el in list(root.iter(*INLINE_WRAPPERS)):
        if el is not root:
            el.drop_tag()
    # 从里往外拆，拆外层时里层已经是干净的块序列
    for el in reversed(list(root.iter(*BLOCK_CONTAINERS))):
        if el is root:
            continue
        parent = el.getparent()
        if parent is not None and (parent is root or parent.tag in BLOCK_CONTAINERS):
            blocks = _group(el)
            for child in list(el):
                el.remove(child)
            el.text = None
            el.extend(blocks)
        elif el.getnext() is not None or (el.tail or "").strip():
            # 在 <li>、<td> 这类内容元素里面：只拆标签，末尾补个空格免得和后文粘连
            el.tail = " " + (el.tail or "")
        el.drop_tag()


def _has_content(el: HtmlElement) -> bool:
    if el.tag in SELF_MEANINGFUL:
        return True
    if (el.text_content() or "").strip():
        return True
    return any(isinstance(d.tag, str) and d.tag in SELF_MEANINGFUL for d in el.iterdescendants())


def _remove_empty(root: HtmlElement, keep_ids: set[str]) -> None:
    # 从里往外删，外层元素在里层被删空后也能被发现
    for el in reversed(list(root.iterdescendants())):
        if not isinstance(el.tag, str):
            _drop(el)  # 注释、处理指令
            continue
        if el.get("id") in keep_ids or _is_mathml(el):
            continue
        if not _has_content(el):
            _drop(el)


def _strip_attrs(root: HtmlElement, keep_ids: set[str]) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str) or _is_mathml(el):
            continue
        allowed = ALLOWED_ATTRS.get(el.tag, frozenset())
        for attr in list(el.attrib):
            if attr == "id" and el.get("id") in keep_ids:
                continue
            if attr not in allowed:
                del el.attrib[attr]


def _strip_comments(root: HtmlElement) -> None:
    for c in root.xpath(".//comment() | .//processing-instruction()"):
        c.drop_tree()


def referenced_ids(roots: Sequence[HtmlElement]) -> set[str]:
    """页内链接（href="#xxx"）指向的 id，比如脚注和回链。这些 id 要保留，否则链接失效。"""
    out: set[str] = set()
    for root in roots:
        for a in root.iter("a"):
            href = a.get("href") or ""
            if href.startswith("#") and len(href) > 1:
                out.add(href[1:])
    return out


def prepare(container: HtmlElement, base_url: str, remove: Sequence[str]) -> HtmlElement:
    """第一步：拷贝容器、删噪音、修图片和链接。属性清理要等所有容器都处理完再做。"""
    root = copy.deepcopy(container)
    root.tail = None
    _strip_comments(root)
    remove_noise(root, remove)
    _noscript_images(root)
    _lazy_images(root)
    _links(root, base_url)
    return root


def finish(root: HtmlElement, keep_ids: set[str]) -> HtmlElement:
    """第二步：拆布局容器、删空元素、清属性。"""
    _unwrap(root)
    _remove_empty(root, keep_ids)
    _strip_attrs(root, keep_ids)
    return root


_EDGE_WS_OPEN = re.compile(r"^(<[^>]+>)\s+")
_EDGE_WS_CLOSE = re.compile(r"\s+(</[^>]+>)$")


def _serialize(el: HtmlElement) -> str:
    html = etree.tostring(el, encoding="unicode", method="html", with_tail=False)
    html = normalize_html(html)
    if el.tag != "pre":
        # "<p> 文字 </p>" 和 "<p>文字</p>" 是同一段，统一成后者
        html = _EDGE_WS_CLOSE.sub(r"\1", _EDGE_WS_OPEN.sub(r"\1", html))
    return html


def block_text(html: str) -> str:
    """段落 HTML 的纯文字（折叠空白），给质检和按文字过滤用。"""
    return " ".join(fragment_fromstring(html, create_parent=True).text_content().split())


def _wrap_run(run: list[HtmlElement | str]) -> HtmlElement | None:
    """一串零散的文字和行内元素包成一个 <p>；只有空白或 <br> 时返回 None。"""
    p = fragment_fromstring("<p></p>")
    last: HtmlElement | None = None
    for item in run:
        if isinstance(item, str):
            if last is None:
                p.text = (p.text or "") + item
            else:
                last.tail = (last.tail or "") + item
        else:
            p.append(item)
            last = item
    # 开头结尾的 <br> 只是空行
    while len(p) and p[0].tag == "br" and not (p.text or "").strip():
        p.text = (p.text or "") + (p[0].tail or "")
        p.remove(p[0])
    while len(p) and p[-1].tag == "br" and not (p[-1].tail or "").strip():
        p.remove(p[-1])
    return p if _has_content(p) else None


def _group(container: HtmlElement) -> list[HtmlElement]:
    """把容器的直接内容整理成块级元素序列：块级子元素原样，相邻的零散内容合成一个 <p>。"""
    out: list[HtmlElement] = []
    run: list[HtmlElement | str] = []

    def flush() -> None:
        if run:
            p = _wrap_run(run)
            run.clear()
            if p is not None:
                out.append(p)

    if container.text:
        run.append(container.text)
    for child in list(container):
        tail, child.tail = child.tail, None
        container.remove(child)
        if child.tag in BLOCK_TAGS or child.tag in BLOCK_CONTAINERS:
            flush()
            out.append(child)
        else:
            run.append(child)  # 行内元素和 <br>；元素之间的空格也要保留
        if tail:
            run.append(tail)
    flush()
    container.text = None
    return out


def split_blocks(root: HtmlElement) -> list[str]:
    """顶层块级元素各成一段；夹在中间的零散文字和行内元素合成一个 <p>。

    容器本身就是块级元素（比如规则直接选中一个 <h2>）时，整个容器就是一段。
    """
    if root.tag in BLOCK_TAGS:
        return [_serialize(root)] if _has_content(root) else []
    return [_serialize(b) for b in _group(root) if _has_content(b)]


def to_segments(blocks: Sequence[str]) -> tuple[Segment, ...]:
    return tuple(Segment(idx=i, html=h) for i, h in enumerate(blocks))
