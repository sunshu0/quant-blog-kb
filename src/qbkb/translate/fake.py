"""确定性的假翻译后端：测试和 Phase 2 串联用，不花额度。"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from qbkb.models import QuotaExceededError
from qbkb.translate.base import count_chars
from qbkb.translate.glossary import case_variants

_TAG = re.compile(r"(<[^>]*>)")


class FakeTranslator:
    """给每个文本节点加前缀、按术语表替换术语，标签原样保留。

    - quota_chars：模拟额度，累计字符数超过它就抛 QuotaExceededError（整批不计费，和 DeepL 一样）；
    - calls / contexts：记录每次调用送进来的段落和上下文，方便断言；
    - chars_used：累计"计费"字符数。
    """

    def __init__(
        self, *, name: str = "fake", prefix: str = "【译】", quota_chars: int | None = None
    ) -> None:
        self._name = name
        self.prefix = prefix
        self.quota_chars = quota_chars
        self.chars_used = 0
        self.calls: list[list[str]] = []
        self.contexts: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        cost = count_chars(segments)
        if self.quota_chars is not None and self.chars_used + cost > self.quota_chars:
            raise QuotaExceededError(f"假后端额度用完（{self.chars_used}/{self.quota_chars}）")
        self.chars_used += cost
        self.calls.append(list(segments))
        self.contexts.append(context)
        return [self.fake_translate(s, glossary) for s in segments]

    def fake_translate(self, html: str, glossary: Mapping[str, str]) -> str:
        parts = _TAG.split(html)
        # split 带捕获组：奇数下标是标签，偶数下标是文本
        out = [
            p if i % 2 or not p.strip() else self._text(p, glossary) for i, p in enumerate(parts)
        ]
        return "".join(out)

    def _text(self, text: str, glossary: Mapping[str, str]) -> str:
        # 和 DeepL 一样：用同一份大小写变体、区分大小写匹配（"DRAWDOWN" 不会命中）
        entries = case_variants(glossary)
        # 长术语优先，避免 "trend following" 先被 "trend" 之类的短词吃掉
        for src in sorted(entries, key=len, reverse=True):
            text = re.sub(rf"\b{re.escape(src)}\b", entries[src], text)
        return self.prefix + text
