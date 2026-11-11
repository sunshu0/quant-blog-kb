"""按 <br> 分行的段落（诗歌、脚注列表）拆成一行一个翻译单元，译完再拼回去。

人工验收发现：整段送给 DeepL 时它会按中文语序把字挪过 <br>，引号、书名号跑到下一行行首
（"《……⏎》7."）。一行一行送就不会跨行。只在"确实是分行排版"的段落上拆：
至少两个 <br>（三行以上），或者每行都以 "1. " 这样的编号开头（脚注列表）。
一个 <br> 的普通段落（句中换行）不拆，免得把一句话拆成两半翻。

这一步只发生在 DeepL 后端里：翻译记忆、结构校验看到的仍然是整段。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from qbkb.translate.structure import VOID

_PREFIX = re.compile(r"^(?:<([a-z][a-z0-9]*)>)+")
_OPEN_TAG = re.compile(r"<([a-z][a-z0-9]*)>")
_BR = re.compile(r"(<br\s*/?>\s*)", re.IGNORECASE)
_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*?(/?)>")
_NUMBERED = re.compile(r"^\d{1,3}\.\s")


@dataclass(frozen=True, slots=True)
class LineSplit:
    prefix: str  # 外层的开始标签，比如 "<p><cite>"
    suffix: str  # 对应的结束标签，比如 "</cite></p>"
    parts: tuple[str, ...]  # 交替出现：行、分隔（<br> 加后面的空白）、行……

    @property
    def lines(self) -> list[str]:
        """要送去翻译的单元：每个非空行包上外层标签。"""
        return [self.prefix + p.strip() + self.suffix for p in self.parts[::2] if p.strip()]

    def join(self, translated: Sequence[str]) -> str:
        """把逐行的译文拼回整段。译文没保住外层标签时原样拼进去（结构校验会拦下）。"""
        it = iter(translated)
        out: list[str] = []
        for i, part in enumerate(self.parts):
            if i % 2 or not part.strip():
                out.append(part)
                continue
            t = next(it)
            if t.startswith(self.prefix) and t.endswith(self.suffix):
                t = t[len(self.prefix) : len(t) - len(self.suffix)]
            out.append(t)
        return self.prefix + "".join(out) + self.suffix


def _br_at_top_level(middle: str) -> bool:
    """middle 里的 <br> 是否都不在别的元素里面（<em>甲<br>乙</em> 这种不拆）。"""
    depth = 0
    for m in _TAG.finditer(middle):
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if name == "br":
            if depth:
                return False
            continue
        if name in VOID or selfclose:
            continue
        depth += -1 if closing else 1
        if depth < 0:
            return False
    return depth == 0


def split_lines(html: str) -> LineSplit | None:
    """能按行拆就返回拆法，否则 None。"""
    m = _PREFIX.match(html)
    if m is None:
        return None
    names = _OPEN_TAG.findall(m.group())
    # 外层取最长的、首尾配对的一串（"<p><cite><b>标题</b><br>……" 的外层是 <p><cite>）
    for k in range(len(names), 0, -1):
        prefix = "".join(f"<{n}>" for n in names[:k])
        suffix = "".join(f"</{n}>" for n in reversed(names[:k]))
        middle = html[len(prefix) : len(html) - len(suffix)]
        paired = html.endswith(suffix) and len(html) > len(prefix) + len(suffix)
        if paired and _br_at_top_level(middle):
            break
    else:
        return None
    parts = tuple(_BR.split(middle))
    lines = [p.strip() for p in parts[::2] if p.strip()]
    if len(lines) < 2 or not _br_at_top_level(middle):
        return None
    if len(parts) < 5 and not all(_NUMBERED.match(line) for line in lines):
        return None
    return LineSplit(prefix, suffix, parts)
