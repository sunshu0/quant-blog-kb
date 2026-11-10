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


def to_markdown(html: str) -> str:
    """一个块级 HTML 片段 → Markdown（去掉首尾空行）。"""
    return _CONVERTER.convert(html).strip()


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


def frontmatter_text(data: Mapping[str, object]) -> str:
    body = yaml.safe_dump(dict(data), allow_unicode=True, sort_keys=False, width=1000)
    return f"---\n{body}---\n"


def _join(blocks: Sequence[str]) -> str:
    return "\n\n".join(b for b in blocks if b)


def render(doc: ArticleDoc) -> str:
    """纯中文主文件。"""
    body = _join(doc.zh_blocks[: doc.n_body])
    appendix = _join(doc.zh_blocks[doc.n_body :])
    parts = [frontmatter_text(doc.frontmatter), f"# {doc.title_zh}", doc.source_line, body]
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
        f"# {doc.title_zh}",
        f"*{title_en}*" if title_en and title_en != doc.title_zh else "",
        doc.source_line,
    ]
    return "\n\n".join(p.strip("\n") for p in [*head, *pairs] if p) + "\n"
