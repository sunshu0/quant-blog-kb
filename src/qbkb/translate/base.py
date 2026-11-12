"""翻译后端的公共部分：请求大小上限、异常、分批、字符计数。"""

from __future__ import annotations

from collections.abc import Sequence

# DeepL API Free 每月额度（字符）的保守回退值：只在 usage() 取不到时用。
# 实际上限以 usage() 返回的为准（各账户不一样，我们的账户 2026-11 实测是 1,000,000）。
FREE_MONTHLY_CHARS = 500_000
# 单次请求的上限，pipeline 分批和 DeepLTranslator 共用这一处定义。
# DeepL 限制：最多 50 条文本、请求体最多 128 KiB；字符上限留足余量给 JSON 转义和 context。
MAX_TEXTS_PER_REQUEST = 50
MAX_REQUEST_CHARS = 30_000


class TranslatorUnavailableError(RuntimeError):
    """后端暂时不可用（限流、网络、5xx），重试用尽。稍后再跑即可。"""


class SegmentTooLargeError(ValueError):
    """单个段落超过单次请求上限，没法送出去。"""


class BackendAuthError(RuntimeError):
    """后端鉴权失败（key 错、无权限、账号不可用）。整个后端都不可用，停下并退出码 1。"""


class ArticleTranslationError(RuntimeError):
    """这一篇（这一批）的请求有问题：后端 4xx、返回条数不符、返回格式看不懂。

    是这篇文章自身的问题，不是整个后端坏了：上层应对这一篇记录失败（needs_review）后
    继续下一篇，不要因此停下整轮。
    """


def count_chars(texts: Sequence[str]) -> int:
    """估算计费字符数：按送出去的 HTML 全长算（含标签），是偏保守的上界。"""
    return sum(len(t) for t in texts)


def chunk_texts(
    texts: Sequence[str],
    max_items: int = MAX_TEXTS_PER_REQUEST,
    max_chars: int = MAX_REQUEST_CHARS,
) -> list[range]:
    """把 texts 按顺序切成若干批，每批不超过 max_items 条、max_chars 个字符。

    单条就超过 max_chars 的抛 SegmentTooLargeError：调用方应该事先把它挑出来。
    """
    if max_items < 1 or max_chars < 1:
        raise ValueError("max_items 和 max_chars 必须是正数")
    chunks: list[range] = []
    start, size = 0, 0
    for i, text in enumerate(texts):
        n = len(text)
        if n > max_chars:
            raise SegmentTooLargeError(f"第 {i} 条有 {n} 个字符，超过单次请求上限 {max_chars}")
        if i > start and (i - start >= max_items or size + n > max_chars):
            chunks.append(range(start, i))
            start, size = i, 0
        size += n
    if start < len(texts):
        chunks.append(range(start, len(texts)))
    return chunks
