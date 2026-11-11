"""脚注上标的占位符：翻译前把 <sup>3</sup> 换成 DeepL 不会丢的写法，译后还原。

2026-11-11 真实冒烟发现：HTML 模式下 DeepL 会把 "指数<sup>2</sup>" 的上标整个丢掉，或者只丢
标签、把数字并进正文（"零折现率3条件下"）。加 ignore_tags=["sup"] 之后第二种情况照样出现。
带 href 的链接 DeepL 保留得很好，所以把纯数字的上标包成一个指向 "#qbkb-sup-<数字>" 的链接送出去。

2026-11-12 第一批 50 篇的人工验收又发现：占位链接的文字如果是数字本身（PR #8 的写法
<a href="#qbkb-sup-13">13</a>），DeepL 会把它当成正文里的数：
- 另外写一个 Unicode 上标（"指标³" + 还原出来的 <sup>3</sup> = "指标³<sup>3</sup>"，10 处）；
- 和前面的数字连读（"2008" + "13" 被译成"2008 至 2013 年"）；
- 小标题里复制一份放到句末（"……估值<sup>5</sup>……5"）。
所以链接文字改成不含数字的记号 SUP_MARK（编号只放在 href 里），译文里的记号还原时去掉。
DeepL 可能把旁边的字一起包进链接（"折现<a …>率†</a>"），还原时把链接里多出来的字放回链接外面。
"""

from __future__ import annotations

import re

from qbkb.translate.structure import (
    SUPERSCRIPT_DIGITS,
    UNICODE_SUP_RUN,
    numeric_sups,
    sup_pattern,
)

SUP_HREF_PREFIX = "#qbkb-sup-"
# 占位链接的文字：不含数字、DeepL 不会翻译或改写的符号
SUP_MARK = "†"

# 只处理没有属性、内容是纯数字的上标：这样往返是无损的
_NUMERIC_SUP = re.compile(r"<sup>(\d{1,4})</sup>")
_A_OPEN = re.compile(r"<a\b", re.IGNORECASE)
_A_CLOSE = re.compile(r"</a\s*>", re.IGNORECASE)
_PLACEHOLDER = re.compile(
    r"<a\s[^>]*?href=([\"'])" + re.escape(SUP_HREF_PREFIX) + r"(\d{1,4})\1[^>]*>(.*?)</a\s*>",
    re.IGNORECASE | re.DOTALL,
)
_TO_ASCII = str.maketrans(SUPERSCRIPT_DIGITS, "0123456789")
_FROM_ASCII = str.maketrans("0123456789", SUPERSCRIPT_DIGITS)
# 脚注和 Unicode 上标之间允许夹着的字符（DeepL 常把上标放在标点前后）
_GAP = r"[\s，,。.；;：:、）)]{0,3}"


def _inside_link(html: str, pos: int) -> bool:
    before = html[:pos]
    return len(_A_OPEN.findall(before)) > len(_A_CLOSE.findall(before))


def protect_sups(html: str) -> str:
    """纯数字的 <sup>N</sup> → <a href="#qbkb-sup-N">†</a>。

    已经在链接里面的上标不动（链接不能嵌套）；带属性的、内容不是纯数字的上标也不动。
    """

    def sub(m: re.Match[str]) -> str:
        if _inside_link(html, m.start()):
            return m.group()
        return f'<a href="{SUP_HREF_PREFIX}{m.group(1)}">{SUP_MARK}</a>'

    return _NUMERIC_SUP.sub(sub, html)


def restore_sups(html: str, *, keep_marks: bool = False) -> str:
    """protect_sups 的逆操作，容忍 DeepL 把旁边的字包进了占位链接。

    编号以 href 为准；链接文字里的记号去掉，多出来的字放回原位（在上标前面）。
    keep_marks=False（原文里没有 † 时）还会去掉 DeepL 挪到链接外面的零散记号。
    占位链接被整个丢掉时无法还原：译文里少一个 <sup>，structure.same_structure 会拦下这一段，
    重翻一次，还不行就标 rejected、文章进 needs_review。
    """

    def sub(m: re.Match[str]) -> str:
        n, inner = m.group(2), m.group(3)
        sup = f"<sup>{n}</sup>"
        # 先认记号：上标放在记号的位置。链接文字里另有数字时，那是 DeepL 拉进来的正文，
        # 不能当编号吃掉
        head, found, tail = inner.partition(SUP_MARK)
        if found:
            return head + sup + tail.replace(SUP_MARK, "")
        # 没有记号：兼容 PR #8 的写法（链接文字是数字本身），也兼容 DeepL 留下的数字
        if inner.endswith(n):
            return inner[: -len(n)] + sup
        if inner.startswith(n):
            return sup + inner[len(n) :]
        head, found, tail = inner.partition(n)
        return head + sup + tail if found else inner + sup

    out = _PLACEHOLDER.sub(sub, html)
    return out if keep_marks else out.replace(SUP_MARK, "")


def sent_length(html: str) -> int:
    """这一段实际送给 DeepL 的长度（脚注换成占位链接以后），用来判断是否超过单次请求上限。"""
    return len(protect_sups(html))


def repair_marks(src_html: str, tgt_html: str) -> str:
    """修掉译文里 DeepL 自己写出来的 Unicode 上标（只做确定无误的两种）：

    1. 和同一个编号的 <sup>N</sup> 紧挨着（中间至多夹几个标点 / 空白）："指标³<sup>3</sup>。"
       → "指标<sup>3</sup>。"。上标放在 Unicode 上标原来的位置，也就是 DeepL 选的位置。
    2. 原文有 <sup>N</sup>、译文里没有，但有一个 Unicode 上标 N："技巧²⁴" → "技巧<sup>24</sup>"。

    其余情况（比如 "2008 至 20<sup>13</sup> 年……¹³"，两处离得远）不动，交给结构校验拦下。
    原文里本来就有 Unicode 上标数字（"m²"）时整段不动。
    """
    if UNICODE_SUP_RUN.search(src_html) or not UNICODE_SUP_RUN.search(tgt_html):
        return tgt_html
    out = tgt_html
    for run in sorted(set(UNICODE_SUP_RUN.findall(out)), key=len, reverse=True):
        n = run.translate(_TO_ASCII)
        u = re.escape(run)
        s = sup_pattern(n)  # 和结构校验同一套识别规则（允许 <sup> 里有空白）
        out = re.sub(rf"(?<![{SUPERSCRIPT_DIGITS}]){u}({_GAP})({s})", r"\2\1", out)
        out = re.sub(rf"({s})({_GAP}){u}(?![{SUPERSCRIPT_DIGITS}])", r"\2\1", out)
    src_sups, out_sups = numeric_sups(src_html), numeric_sups(out)
    for n in sorted(set(src_sups)):
        k = src_sups.count(n) - out_sups.count(n)
        if k <= 0:
            continue
        run = n.translate(_FROM_ASCII)
        pat = re.compile(rf"(?<![{SUPERSCRIPT_DIGITS}]){re.escape(run)}(?![{SUPERSCRIPT_DIGITS}])")
        if len(pat.findall(out)) == k:
            out = pat.sub(f"<sup>{n}</sup>", out)
    return out
