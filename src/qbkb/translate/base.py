"""翻译后端的公共部分：上下文协议、分批、字符计数。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

# DeepL API Free 每月额度（字符）
FREE_MONTHLY_CHARS = 500_000
# DeepL 单次请求最多 50 条文本
MAX_TEXTS_PER_REQUEST = 50


@runtime_checkable
class SupportsContext(Protocol):
    """可以接收参考上下文的后端（models.Translator 的扩展，不改共享协议）。

    context 是前后文的纯文本，只作参考，不翻译；DeepL 不对 context 计费。
    """

    def translate_with_context(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str
    ) -> list[str]: ...


def count_chars(texts: Sequence[str]) -> int:
    """估算计费字符数：按送出去的 HTML 全长算（含标签），是偏保守的上界。"""
    return sum(len(t) for t in texts)


def chunk_texts(texts: Sequence[str], max_items: int, max_chars: int) -> list[range]:
    """把 texts 按顺序切成若干批，每批不超过 max_items 条、max_chars 个字符。

    单条就超过 max_chars 的，自己单独成一批（交给后端报错，不在这里截断）。
    """
    if max_items < 1 or max_chars < 1:
        raise ValueError("max_items 和 max_chars 必须是正数")
    chunks: list[range] = []
    start, size = 0, 0
    for i, text in enumerate(texts):
        n = len(text)
        if i > start and (i - start >= max_items or size + n > max_chars):
            chunks.append(range(start, i))
            start, size = i, 0
        size += n
    if start < len(texts):
        chunks.append(range(start, len(texts)))
    return chunks
