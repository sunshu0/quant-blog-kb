"""脚注上标的占位符：翻译前把 <sup>3</sup> 换成 DeepL 不会丢的写法，译后还原。

2026-11-11 真实冒烟发现：HTML 模式下 DeepL 会把 "指数<sup>2</sup>" 的上标整个丢掉，或者只丢
标签、把数字并进正文（"零折现率3条件下"）。加 ignore_tags=["sup"] 之后第二种情况照样出现。
带 href 的链接 DeepL 保留得很好，所以把纯数字的上标包成一个指向 "#qbkb-sup-<数字>" 的链接送出去。
DeepL 可能把旁边的字一起包进链接（"折现<a …>率3</a>"），还原时以 href 里的数字为准，
链接里多出来的字放回链接外面。
"""

from __future__ import annotations

import re

SUP_HREF_PREFIX = "#qbkb-sup-"

# 只处理没有属性、内容是纯数字的上标：这样往返是无损的
_NUMERIC_SUP = re.compile(r"<sup>(\d{1,4})</sup>")
_A_OPEN = re.compile(r"<a\b", re.IGNORECASE)
_A_CLOSE = re.compile(r"</a\s*>", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"<a\s[^>]*?href=([\"'])" + re.escape(SUP_HREF_PREFIX) + r"(\d{1,4})\1[^>]*>(.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _inside_link(html: str, pos: int) -> bool:
    before = html[:pos]
    return len(_A_OPEN.findall(before)) > len(_A_CLOSE.findall(before))


def protect_sups(html: str) -> str:
    """纯数字的 <sup>N</sup> → <a href="#qbkb-sup-N">N</a>。

    已经在链接里面的上标不动（链接不能嵌套）；带属性的、内容不是纯数字的上标也不动。
    """

    def sub(m: re.Match[str]) -> str:
        if _inside_link(html, m.start()):
            return m.group()
        n = m.group(1)
        return f'<a href="{SUP_HREF_PREFIX}{n}">{n}</a>'

    return _NUMERIC_SUP.sub(sub, html)


def restore_sups(html: str) -> str:
    """protect_sups 的逆操作，容忍 DeepL 把旁边的字包进了占位链接。

    数字以 href 为准：链接文字里数字前后多出来的字放回原位；链接文字里连数字都没有了，
    就把剩下的字放在上标前面。占位链接被整个丢掉时无法还原，结构校验会拦下这一段。
    """

    def sub(m: re.Match[str]) -> str:
        n, inner = m.group(2), m.group(3)
        sup = f"<sup>{n}</sup>"
        if inner.endswith(n):
            return inner[: -len(n)] + sup
        if inner.startswith(n):
            return sup + inner[len(n) :]
        head, found, tail = inner.partition(n)
        return head + sup + tail if found else inner + sup

    return _PLACEHOLDER.sub(sub, html)
