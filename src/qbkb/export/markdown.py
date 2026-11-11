"""HTML 段落 → Markdown，术语首次出现附英文，frontmatter 和整篇拼装。都是纯函数。"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

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


def safe_url(url: str) -> str:
    """链接 / 图片地址里的括号、空格、尖括号转成 %xx，Markdown 链接不会断。"""
    return "".join(_URL_UNSAFE.get(c, c) for c in url)


def _safe_url_attrs(html: str) -> str:
    def sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{m.group(2)}{safe_url(m.group(3))}{m.group(2)}"

    parts = _TAG.split(html)
    for i in range(1, len(parts), 2):  # 奇数下标是标签
        parts[i] = _URL_ATTR.sub(sub, parts[i])
    return "".join(parts)


def to_markdown(html: str) -> str:
    """一个块级 HTML 片段 → Markdown（去掉首尾空行）。

    结果里不含原始 HTML（上下标除外），链接地址里的括号、空格已转义。
    """
    return _escape_html(_CONVERTER.convert(_safe_url_attrs(html)).strip())


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

    def __init__(self) -> None:
        self.done: set[str] = set()

    def annotate(self, zh_html: str, terms: Mapping[str, str]) -> str:
        todo = {
            en: zh
            for en, zh in terms.items()
            if zh != en and en not in self.done and zh not in self.done
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
                pos = part.find(zh)
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
