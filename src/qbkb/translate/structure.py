"""译文结构校验和纯文本提取（D7 分级，design.md 4.5）。

- **严格**：sup（脚注编号的集合和次数都要一致：不丢、不重复）、img（src）、math、code / pre、
  table 及其子结构，以及其余没有单独列出的标签（标题、列表、cite、br……）。
- **按地址**：a 只比较 href 的集合。DeepL 把一个链接拆成两段、指向同一个地址不算错；
  多出或少了某个地址算错。
- **放宽**：em、i、strong、b 不比数量（DeepL 会按中文语序拆开或合并强调）。
"""

from __future__ import annotations

import re
from collections import Counter
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
# 放宽：不比数量和位置，只要求正确闭合
RELAXED = frozenset({"em", "i", "strong", "b"})
# 按地址：只比 href 集合
BY_ADDRESS = frozenset({"a"})
# 结构指纹里不出现的标签（放宽的和按地址的）
TRANSPARENT = RELAXED | BY_ADDRESS

# Unicode 上标数字：DeepL 有时把脚注编号写成 ¹²³，校验时和 <sup>N</sup> 一样算一个脚注
SUPERSCRIPT_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_TO_ASCII = str.maketrans(SUPERSCRIPT_DIGITS, "0123456789")
UNICODE_SUP_RUN = re.compile(f"[{SUPERSCRIPT_DIGITS}]+")

# 文本里标出脚注位置用的私用区字符（不会出现在正文里）
_OPEN, _CLOSE = chr(0xE000), chr(0xE001)
# 段末可以跟在数字后面的标点和空白
_TRAILING = " \t\r\n。．.，,；;：:！!？?）)」』\"”’'》】"


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: str = ""
    closed: bool = True
    children: list[_Node] = field(default_factory=list)

    def tokens(self) -> list[str]:
        """结构指纹的组成部分。透明标签（放宽 / 按地址）只留下子元素，没闭合的另记一笔。"""
        kids = [t for c in self.children for t in c.tokens()]
        if self.tag in TRANSPARENT:
            return kids if self.closed else [f"{self.tag}!", *kids]
        # 兄弟元素排序后再比较：中英文语序不同，DeepL 会合理地调换行内标签的先后
        mark = "" if self.closed else "!"
        return [f"{self.tag}{mark}[{self.attrs}]({','.join(sorted(kids))})"]


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root")
        self.stack = [self.root]
        self.text: list[str] = []
        self.marked: list[str] = []  # 正文文字，<sup> 的内容用 _OPEN / _CLOSE 包起来
        self.sups: list[str] = []  # 每个 <sup> 里的文字（去掉空白）
        self.hrefs: set[str] = set()
        self.outside: list[str] = []  # 不在 <sup> 里的文字
        self.sups_balanced = True  # <sup> 没有嵌套、没有多余的结束标签、都闭合了
        self._open_sups: list[list[str]] = []

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        key = " ".join(
            f"{n}={v}" for n, v in sorted(attrs, key=lambda a: a[0]) if n in ("href", "src")
        )
        if tag == "a":
            self.hrefs.add(next((v or "" for n, v in attrs if n == "href"), ""))
        node = _Node(tag, key)
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add(tag, attrs)
        if tag not in VOID:
            self.stack.append(node)
        if tag == "sup":
            if self._open_sups:
                self.sups_balanced = False  # 上标里套上标
            self._open_sups.append([])
            self.marked.append(_OPEN)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # <br/> 和 <p/> 都按不入栈的空元素处理（默认实现会再补一个结束标签）
        self._add(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            return
        if tag == "sup":
            if self._open_sups:
                self.sups.append(_WS.sub("", "".join(self._open_sups.pop())))
                self.marked.append(_CLOSE)
            else:
                self.sups_balanced = False  # 多出来的 </sup>
        if not any(n.tag == tag for n in self.stack[1:]):
            # 多出来的结束标签。放宽的和按地址的标签多一个结束标签不影响显示，不算
            if tag not in TRANSPARENT:
                self.stack[-1].children.append(_Node(f"/{tag}"))
            return
        while True:
            node = self.stack.pop()
            if node.tag == tag:
                return
            node.closed = False  # 被外层结束标签强行关掉的元素

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        self.marked.append(data)
        if not self._open_sups:
            self.outside.append(data)
        for buf in self._open_sups:
            buf.append(data)

    def close(self) -> None:
        super().close()
        for node in self.stack[1:]:
            node.closed = False
        while self._open_sups:  # 没闭合的上标也算
            self.sups_balanced = False
            self.sups.append(_WS.sub("", "".join(self._open_sups.pop())))


def _parse(html: str) -> _TreeParser:
    parser = _TreeParser()
    parser.feed(html)
    parser.close()
    return parser


def structure_signature(html: str) -> str:
    """片段的结构指纹：严格标签的嵌套关系、是否正确闭合，以及图片（src）地址。

    文本不算；同一层兄弟元素的先后顺序不算；em / i / strong / b 和 a 是透明的
    （只看它们的子元素，a 的地址另外按集合比较，见 link_targets）。
    """
    return ",".join(sorted(_parse(html).root.tokens()))


def link_targets(html: str) -> set[str]:
    """片段里所有链接的 href（集合）。"""
    return _parse(html).hrefs


def footnote_marks(html: str) -> list[str]:
    """片段里的脚注编号，按字符串排好序：纯数字的 <sup>，加上正文里的 Unicode 上标数字（¹³ → 13）。

    重复的编号出现几次就列几次，所以 "¹<sup>1</sup>" 是 ["1", "1"]。
    """
    return _marks(_parse(html))


def _marks(p: _TreeParser) -> list[str]:
    plain = "".join(p.outside)  # 解析器记下的 <sup> 外文字，嵌套 / 没闭合也不会错位
    uni = [m.group().translate(_TO_ASCII) for m in UNICODE_SUP_RUN.finditer(plain)]
    return sorted([s for s in p.sups if s.isdigit()] + uni)


def numeric_sups(html: str) -> list[str]:
    """纯数字 <sup> 的编号（去掉空白："<sup>24 </sup>" 是 24），按出现顺序。

    结构校验和 footnotes.repair_marks 用同一套识别规则（另见 sup_pattern）。
    """
    return [s for s in _parse(html).sups if s.isdigit()]


def sup_pattern(n: str) -> str:
    """匹配编号 n 的 <sup> 的正则（允许前后空白），和 numeric_sups 的识别一致。没有捕获组。"""
    return rf"<sup>\s*{re.escape(n)}\s*</sup>"


def _bare_numbers(text: str) -> Counter[str]:
    """文字里独立的数字串（不是更长数字的一部分）。"""
    return Counter(re.findall(r"(?<![\d.,])\d+(?![\d.,]*\d)", text))


def _stray_numbers(src: _TreeParser, tgt: _TreeParser) -> set[str]:
    """译文里紧挨着脚注、或者在段末的裸数字，和某个脚注编号相同（"估值<sup>5</sup>5"、"……估值5"）。

    DeepL 把脚注编号当成正文数字复制一份时就是这两种形状。只检查原文正文里**没有**这个数字的
    编号（原文里它只作为脚注出现），原文本来就有的数字（"grew by 3<sup>3</sup>"）不算，免得误报。
    """
    body = _bare_numbers("".join(src.outside))
    marked = "".join(tgt.marked)
    tail = marked.rstrip(_TRAILING)
    out: set[str] = set()
    for n in {s for s in src.sups if s.isdigit()}:
        if body[n]:
            continue
        sup = re.escape(f"{_OPEN}{n}{_CLOSE}")
        bare = re.escape(n)
        adjacent = rf"(?<![\d.,]){bare}\s*{sup}|{sup}\s*{bare}(?![\d.,])"
        if re.search(adjacent, marked) or re.search(rf"(?<![\d.,{_OPEN}]){bare}$", tail):
            out.add(n)
    return out


def same_structure(src_html: str, tgt_html: str) -> bool:
    """翻译前后结构是否一致（D7 分级，见模块说明）。

    脚注编号单独再比一次（footnote_marks）：结构指纹不看文字，上标还在但数字变了、
    数字跑到了上标外面、或者 DeepL 另外写了一个 Unicode 上标，指纹是看不出来的。
    译文里 <sup> 嵌套、没闭合或多一个结束标签时，脚注无法可靠比较，直接判为不一致。
    """
    src, tgt = _parse(src_html), _parse(tgt_html)
    if ",".join(sorted(src.root.tokens())) != ",".join(sorted(tgt.root.tokens())):
        return False
    if src.hrefs != tgt.hrefs:
        return False
    if src.sups_balanced and not tgt.sups_balanced:
        return False
    if _marks(src) != _marks(tgt):
        return False
    return not _stray_numbers(src, tgt)


def plain_text(html: str) -> str:
    """去掉标签、折叠空白，用作 DeepL 的 context，也用来找段落里的术语。"""
    return _WS.sub(" ", "".join(_parse(html).text)).strip()
