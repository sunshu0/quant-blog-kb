"""译文结构校验和纯文本提取。"""

from __future__ import annotations

import re
from collections import Counter
from html.parser import HTMLParser

_WS = re.compile(r"\s+")


class _SignatureParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.signature: Counter[str] = Counter()
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.signature[f"<{tag}>"] += 1
        for name, value in attrs:
            if name in ("href", "src"):
                self.signature[f"{name}={value}"] += 1

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def _parse(html: str) -> _SignatureParser:
    parser = _SignatureParser()
    parser.feed(html)
    parser.close()
    return parser


def structure_signature(html: str) -> Counter[str]:
    """片段的结构指纹：每种标签的数量，加上每个链接（href）和图片（src）地址的数量。"""
    return _parse(html).signature


def same_structure(src_html: str, tgt_html: str) -> bool:
    """翻译前后标签、链接、图片是否一致。"""
    return structure_signature(src_html) == structure_signature(tgt_html)


def plain_text(html: str) -> str:
    """去掉标签、折叠空白，用作 DeepL 的 context。"""
    return _WS.sub(" ", "".join(_parse(html).text)).strip()
