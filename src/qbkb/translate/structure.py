"""译文结构校验和纯文本提取。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_WS = re.compile(r"\s+")
# 没有结束标签的元素：<br> 和 <br/> 视为同一个东西
VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: str = ""
    closed: bool = True
    children: list[_Node] = field(default_factory=list)

    def canonical(self) -> str:
        # 兄弟元素排序后再比较：中英文语序不同，DeepL 会合理地调换行内标签的先后
        kids = ",".join(sorted(c.canonical() for c in self.children))
        mark = "" if self.closed else "!"
        return f"{self.tag}{mark}[{self.attrs}]({kids})"


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.stack = [self.root]
        self.text: list[str] = []
        self.sups: list[str] = []  # 每个 <sup> 里的文字（去掉空白）
        self._open_sups: list[list[str]] = []

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        key = " ".join(
            f"{n}={v}" for n, v in sorted(attrs, key=lambda a: a[0]) if n in ("href", "src")
        )
        node = _Node(tag, key)
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add(tag, attrs)
        if tag not in VOID:
            self.stack.append(node)
        if tag == "sup":
            self._open_sups.append([])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 和 <p/> 都按不入栈的空元素处理（默认实现会再补一个结束标签）
        self._add(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if tag == "sup" and self._open_sups:
            self.sups.append(_WS.sub("", "".join(self._open_sups.pop())))
        if not any(n.tag == tag for n in self.stack[1:]):
            # 多出来的结束标签
            self.stack[-1].children.append(_Node(f"/{tag}"))
            return
        while True:
            node = self.stack.pop()
            if node.tag == tag:
                return
            node.closed = False  # 被外层结束标签强行关掉的元素

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        for buf in self._open_sups:
            buf.append(data)

    def close(self) -> None:
        super().close()
        for node in self.stack[1:]:
            node.closed = False
        while self._open_sups:  # 没闭合的上标也算
            self.sups.append(_WS.sub("", "".join(self._open_sups.pop())))


def _parse(html: str) -> _TreeParser:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser


def structure_signature(html: str) -> str:
    """片段的结构指纹：标签的嵌套关系、是否正确闭合，以及链接（href）和图片（src）地址。

    文本不算；同一层兄弟元素的先后顺序不算。
    """
    return _parse(html).root.canonical()


def footnote_marks(html: str) -> list[str]:
    """片段里的脚注编号：内容是纯数字的 <sup>，按字符串排好序。"""
    return sorted(s for s in _parse(html).sups if s.isdigit())


def same_structure(src_html: str, tgt_html: str) -> bool:
    """翻译前后标签、链接、图片是否一致，脚注编号是否一个不少、数字不变。

    脚注编号单独再比一次（footnote_marks）：结构指纹不看文字，上标还在但数字变了、
    或者数字跑到了上标外面，指纹是看不出来的。
    """
    if structure_signature(src_html) != structure_signature(tgt_html):
        return False
    return footnote_marks(src_html) == footnote_marks(tgt_html)


def plain_text(html: str) -> str:
    """去掉标签、折叠空白，用作 DeepL 的 context，也用来找段落里的术语。"""
    return _WS.sub(" ", "".join(_parse(html).text)).strip()
