"""HTML 段落 → Markdown，术语首次出现附英文，frontmatter 和整篇拼装。都是纯函数。"""

from __future__ import annotations

import html as htmllib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import yaml
from markdownify import MarkdownConverter

# 拆出标签和文本；文本节点才做术语标注
_TAG = re.compile(r"(<[^>]*>)")
_IMG_SRC = re.compile(r"(<img\b[^>]*?\bsrc=)([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
# 在这些元素里面不做术语标注（代码、公式、链接文字原样保留）
_NO_ANNOTATE = frozenset({"code", "pre", "math", "a", "script", "style"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")

_CONVERTER = MarkdownConverter(
    heading_style="ATX",
    bullets="-",
    sup_symbol="<sup>",
    sub_symbol="<sub>",
    keep_inline_images_in=["td", "th", "a", "li", "span", "figure", "figcaption"],
)


# 转换结果里允许保留的 HTML：上下标（sup_symbol / sub_symbol 产生的）和 markdownify 的自动链接
_ALLOWED_HTML = re.compile(r"</?(?:sup|sub)>|<(?:https?|mailto):[^\s<>]+>", re.IGNORECASE)
# 代码块和行内代码里 Markdown 不解析 HTML，不用（也不能）转义
_CODE = re.compile(r"(```.*?```|`+[^`\n]*`+)", re.DOTALL)
_URL_ATTR = re.compile(r"(\s(?:href|src)=)([\"'])(.*?)\2", re.IGNORECASE | re.DOTALL)
# 链接地址里会让 Markdown 的 [文字](地址) 断开的字符
_URL_UNSAFE = {"(": "%28", ")": "%29", " ": "%20", "<": "%3C", ">": "%3E"}


def escape_text(text: str) -> str:
    """纯文本放进 Markdown（标题、来源行）：< 转成 &lt;，不让它变成真正的 HTML。"""
    return text.replace("<", "&lt;")


def _escape_html(md: str) -> str:
    """转换结果里剩下的 < 一律转义，只放行白名单（上下标、自动链接）；代码里不动。

    markdownify 会把原文里转义过的 &lt;script&gt; 还原成 <script>，不处理的话
    Markdown 渲染器会把它当成真正的 HTML。
    """
    parts = _CODE.split(md)
    for i in range(0, len(parts), 2):  # 偶数下标是代码之外的部分
        text, out, pos = parts[i], [], 0
        for m in _ALLOWED_HTML.finditer(text):
            out += [escape_text(text[pos : m.start()]), m.group()]
            pos = m.end()
        out.append(escape_text(text[pos:]))
        parts[i] = "".join(out)
    return "".join(parts)


# 浏览器解析地址前会去掉的字符：控制字符和空白（"java\tscript:" 照样能执行）
_URL_IGNORED = re.compile(r"[\x00-\x20\x7f]+")
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):", re.IGNORECASE)
LINK_SCHEMES = frozenset({"http", "https", "mailto"})
IMAGE_SCHEMES = frozenset({"http", "https"})
_TAG_NAME = re.compile(r"^<\s*([a-zA-Z][a-zA-Z0-9]*)")
_ALT = re.compile(r"\salt=([\"'])(.*?)\1", re.IGNORECASE | re.DOTALL)


def url_allowed(url: str, kind: Literal["link", "image"] = "link") -> bool:
    """协议白名单（纵深防御，extract 已经挡过一次）。

    链接：http、https、mailto、# 锚点、相对路径；图片：http、https、相对路径。
    先还原 HTML 实体、去掉控制字符和空白再判断，所以 JaVaScRiPt:、java\\tscript:、
    &#106;avascript: 这些变体都会被拦下。
    """
    cleaned = _URL_IGNORED.sub("", htmllib.unescape(url))
    if cleaned.startswith("#"):
        return kind == "link"
    m = _SCHEME.match(cleaned)
    if m is None:
        return True  # 相对路径（包括 //host/x 这种跟随页面协议的）
    allowed = LINK_SCHEMES if kind == "link" else IMAGE_SCHEMES
    return m.group(1).lower() in allowed


def safe_url(url: str) -> str:
    """链接 / 图片地址里的括号、空格、尖括号转成 %xx，Markdown 链接不会断。

    只做转义，不做协议检查（见 url_allowed）。
    """
    return "".join(_URL_UNSAFE.get(c, c) for c in url)


def _clean_tag(tag: str) -> str:
    """检查一个标签的 href / src：协议不在白名单里就去掉这个属性；
    图片没了地址就只留 alt 文字（链接去掉 href 后 markdownify 只输出链接文字）。"""
    m = _TAG_NAME.match(tag)
    name = m.group(1).lower() if m else ""
    kind: Literal["link", "image"] = "image" if name == "img" else "link"
    dropped = False

    def sub(a: re.Match[str]) -> str:
        nonlocal dropped
        if not url_allowed(a.group(3), kind):
            dropped = True
            return ""
        return f"{a.group(1)}{a.group(2)}{safe_url(a.group(3))}{a.group(2)}"

    out = _URL_ATTR.sub(sub, tag)
    if dropped and name == "img":
        alt = _ALT.search(tag)
        return htmllib.escape(htmllib.unescape(alt.group(2)), quote=False) if alt else ""
    return out


def _safe_url_attrs(html: str) -> str:
    parts = _TAG.split(html)
    for i in range(1, len(parts), 2):  # 奇数下标是标签
        parts[i] = _clean_tag(parts[i])
    return "".join(parts)


# 一张 Markdown 图片；一整行只有两张以上图片时拆成每张一行（见 split_image_rows）
_IMAGE_MD = r"!\[[^\]\n]*\]\([^)\n]*\)"
_IMAGE = re.compile(_IMAGE_MD)
_IMAGE_ROW = re.compile(rf"{_IMAGE_MD}(?:[ \t]*{_IMAGE_MD})+[ \t]*")
# 中文标点后面紧跟的半角空格 / 制表符（后面还有内容的才算：行尾的两个空格是 Markdown 的硬换行；
# 表格的 | 不动）。全角空格（U+3000）是有意的排版，不删
_ZH_PUNCT_SPACE = re.compile(r"(?<=[。！？；：，、])[ \t]+(?=[^\s|])")
# 链接 / 图片的地址部分：里面的内容不动
_LINK_DEST = re.compile(r"(\]\([^)\n]*\))")
_INLINE_CODE = re.compile(r"(`+[^`\n]*`+)")
_FENCE_LINE = re.compile(r"^ {0,3}(```|~~~)")


def _text_lines(md: str, fn: Callable[[str], str]) -> str:
    """对代码块之外的每一行调用 fn。围栏代码块（``` / ~~~）和缩进代码块（4 个空格或制表符开头）
    原样保留。缩进的列表续行也会被当成代码跳过：宁可少处理，不改代码。"""
    out: list[str] = []
    fence: str | None = None
    for line in md.split("\n"):
        m = _FENCE_LINE.match(line)
        if fence is not None:
            if m and m.group(1) == fence:
                fence = None
        elif m:
            fence = m.group(1)
        elif not line.startswith(("    ", "\t")):
            line = fn(line)
        out.append(line)
    return "\n".join(out)


def split_image_rows(md: str) -> str:
    """块级图片各自单独成段：一行里只有几张图片（"![](a)![](b)"）时，每张拆成一段。

    夹在文字中间的行内图片、表格和列表里的图片、代码里的内容都不动。
    """

    def fix(line: str) -> str:
        # 整行都是图片才拆："`code`![](a)![](b)" 这种前面还有东西的行不动
        return "\n\n".join(_IMAGE.findall(line)) if _IMAGE_ROW.fullmatch(line) else line

    return _text_lines(md, fix)


def tidy_zh_spacing(md: str) -> str:
    """去掉中文标点（。！？；：，、）后面多余的空格：DeepL 的译文里常有"影响。 如果"。

    只用在中文段落上。代码（围栏、缩进、行内）、链接和图片的地址不动；英文标点后面的空格、
    全角空格、行尾的硬换行不动。
    """

    def fix(line: str) -> str:
        pieces = _INLINE_CODE.split(line)
        for i in range(0, len(pieces), 2):  # 偶数下标是行内代码之外的部分
            parts = _LINK_DEST.split(pieces[i])
            for j in range(0, len(parts), 2):  # 偶数下标是链接地址之外的部分
                parts[j] = _ZH_PUNCT_SPACE.sub("", parts[j])
            pieces[i] = "".join(parts)
        return "".join(pieces)

    return _text_lines(md, fix)


def to_markdown(html: str) -> str:
    """一个块级 HTML 片段 → Markdown（去掉首尾空行）。

    结果里不含原始 HTML（上下标除外）；链接和图片只留白名单协议（url_allowed），
    地址里的括号、空格已转义；相邻的块级图片各占一段。
    """
    return split_image_rows(_escape_html(_CONVERTER.convert(_safe_url_attrs(html)).strip()))


def rewrite_images(html: str, replace: Callable[[str], str]) -> str:
    """把 <img src="..."> 的地址换成 replace(原地址) 的结果（下载到本地后的相对路径）。"""

    def sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}{replace(m.group(3))}{m.group(2)}"

    return _IMG_SRC.sub(sub, html)


def image_sources(html: str) -> list[str]:
    return [m.group(3) for m in _IMG_SRC.finditer(html)]


class TermAnnotator:
    """术语第一次出现时附上英文："趋势跟踪" → "趋势跟踪（trend following）"。

    一篇文章用一个实例（记住哪些术语已经标过）。只标原文里确实出现了这个术语的段落，
    译文和原文相同的词条（alpha、CTA）不标；代码、公式、链接文字里不标。
    """

    def __init__(self, all_terms: Mapping[str, str] | None = None) -> None:
        self.done: set[str] = set()
        # 整份术语表的译文：短译文落在更长的术语译文里面时（"波动率"在"隐含波动率"里）不标，
        # 否则会出现"隐含波动率（volatility）"——英文注释挂到了长短语里的短词上
        self._all_zh = sorted({zh for zh in (all_terms or {}).values()}, key=len, reverse=True)

    def _find(self, part: str, zh: str) -> int:
        """zh 在 part 里第一次"独立"出现的位置：不在任何更长的术语译文里面。找不到返回 -1。"""
        longer = [z for z in self._all_zh if len(z) > len(zh) and zh in z]
        covered: list[tuple[int, int]] = []
        for z in longer:
            start = part.find(z)
            while start >= 0:
                covered.append((start, start + len(z)))
                start = part.find(z, start + 1)
        pos = part.find(zh)
        while pos >= 0:
            end = pos + len(zh)
            if not any(a <= pos and end <= b for a, b in covered):
                return pos
            pos = part.find(zh, pos + 1)
        return -1

    def annotate(self, zh_html: str, terms: Mapping[str, str]) -> str:
        todo = {
            en: zh
            for en, zh in terms.items()
            # 带编号的词条（"Exhibit 3" → "图表3"）只是统一译名，不用附英文
            if zh != en
            and en not in self.done
            and zh not in self.done
            and not any(ch.isdigit() for ch in en)
        }
        if not todo:
            return zh_html
        parts = _TAG.split(zh_html)
        depth = 0  # 当前在几层 _NO_ANNOTATE 元素里
        for i, part in enumerate(parts):
            if i % 2:  # 标签
                name = part.strip("</>").split()[0].lower() if part.strip("</>") else ""
                if name in _NO_ANNOTATE and not part.endswith("/>"):
                    depth += -1 if part.startswith("</") else 1
                continue
            if depth > 0 or not part.strip():
                continue
            # 长的中文先标，避免短词抢先（例如"夏普比率"和"比率"）
            for en, zh in sorted(todo.items(), key=lambda t: len(t[1]), reverse=True):
                if en in self.done or zh in self.done:
                    continue
                pos = self._find(part, zh)
                if pos < 0:
                    continue
                end = pos + len(zh)
                if part[end : end + 1] in ("（", "("):
                    self.done.update((en, zh))  # 译文里已经自带英文
                    continue
                part = f"{part[:end]}（{en}）{part[end:]}"
                self.done.update((en, zh))
            parts[i] = part
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class ArticleDoc:
    """拼一篇 Markdown 需要的全部信息。"""

    frontmatter: dict[str, object]
    title_zh: str
    source_line: str
    zh_blocks: Sequence[str]  # 已转成 Markdown 的中文段落
    en_blocks: Sequence[str]  # 对应的英文段落（对照版用）
    n_body: int  # 前 n_body 段是正文，后面是原文的版权声明等附录


def date_part(published: str | None) -> str:
    """文件名里的日期：YYYY-MM-DD，没有就是 undated。"""
    if published and _DATE.match(published):
        return published[:10]
    return "undated"


def md_filename(published: str | None, slug: str) -> str:
    return f"{date_part(published)}_{slug}.md"


class _QuotingDumper(yaml.SafeDumper):
    """含 < > & 的字符串一律加双引号，frontmatter 渲染时也不会被当成 HTML。"""


def _str_representer(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = '"' if any(c in value for c in "<>&") else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_QuotingDumper.add_representer(str, _str_representer)


def frontmatter_text(data: Mapping[str, object]) -> str:
    body = yaml.dump(
        dict(data), Dumper=_QuotingDumper, allow_unicode=True, sort_keys=False, width=1000
    )
    return f"---\n{body}---\n"


def _join(blocks: Sequence[str]) -> str:
    return "\n\n".join(b for b in blocks if b)


def render(doc: ArticleDoc) -> str:
    """纯中文主文件。"""
    body = _join(doc.zh_blocks[: doc.n_body])
    appendix = _join(doc.zh_blocks[doc.n_body :])
    head = f"# {escape_text(doc.title_zh)}"
    parts = [frontmatter_text(doc.frontmatter), head, doc.source_line, body]
    if appendix:
        parts += ["---", appendix]
    return "\n\n".join(p.strip("\n") for p in parts if p) + "\n"


def _plain(md: str) -> str:
    return re.sub(r"\s+", " ", md).strip()


def render_bilingual(doc: ArticleDoc, title_en: str) -> str:
    """对照版：每段英文后面跟中文。两边一样的段落（只有图片、数字）只出现一次。"""
    pairs: list[str] = []
    for i, (en, zh) in enumerate(zip(doc.en_blocks, doc.zh_blocks, strict=True)):
        if i == doc.n_body and i < len(doc.zh_blocks):
            pairs.append("---")
        pairs.append(en if _plain(en) == _plain(zh) else _join([en, zh]))
    head = [
        frontmatter_text(doc.frontmatter),
        f"# {escape_text(doc.title_zh)}",
        f"*{escape_text(title_en)}*" if title_en and title_en != doc.title_zh else "",
        doc.source_line,
    ]
    return "\n\n".join(p.strip("\n") for p in [*head, *pairs] if p) + "\n"
