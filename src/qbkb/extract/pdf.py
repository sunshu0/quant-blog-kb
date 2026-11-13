"""PDF 正文抽取：pypdf 取文字 → 段落。

只用 pypdf（BSD-3-Clause，见 design.md 4.3）按页取纯文本，不去解析排版结构：
- 页眉 / 页脚 / 页码：跨页重复出现在页边的行、以及纯页码行去掉；
- 连字符断行合并：软连字符（U+00AD）删除，普通连字符（- / ‐）保守保留
  （避免把 trend-following 这类复合词改坏成 trendfollowing）；
- 空行切段、翻页也算一段，过长的段落按句号切分；
- 公式 / 表格没有可靠的结构化提取，保留 pypdf 给的原文字，由调用方在 qc_issues 里记提示；
- 扫描版（没有文字层）抛 NoTextLayerError，不做 OCR。
"""

from __future__ import annotations

import io
import logging
import re
from collections import Counter
from collections.abc import Sequence

from pypdf import PdfReader

# pypdf 遇到缺 fontTools 的 CFF Type1 字体会打大量 WARNING（"fontTools is required to
# fully parse..."），只是降级解析、不是错误。把 pypdf 整包的日志级别提到 ERROR：
# 只留它真正的错误，不吞错误、也不让这类非致命警告刷屏。
logging.getLogger("pypdf").setLevel(logging.ERROR)

# 超过这个字符数的段落按句号切分，避免单段太长不好翻译
MAX_PARAGRAPH_CHARS = 2000
# 页眉 / 页脚只看每页最前 / 最后几行
_EDGE_LINES = 3
# 一行在多少比例的页面上出现就算页眉 / 页脚（至少 2 页）
_REPEAT_RATIO = 0.6

_PAGE_NUMBER = re.compile(
    r"^(?:"
    r"\d{1,4}"  # 纯数字
    r"|page\s*\d{1,4}"  # Page 3
    r"|第\s*\d{1,4}\s*页"  # 第 3 页
    r"|\d{1,4}\s*/\s*\d{1,4}"  # 3 / 10
    r"|[-–—]\s*\d{1,4}\s*[-–—]"  # - 3 -
    r")\s*$",
    re.IGNORECASE,
)

# 行尾的页码（可选前导分隔符、可选总页数、page/第 N 页）。把 "www.cfm.fr 02" 归一成
# "www.cfm.fr"，让带页码的页脚能被"跨页重复"检测出来。
_PAGE_NUMBER_TAIL = re.compile(
    r"(?:"
    r"\s*[-–—|/·•]?\s*\d{1,4}\s*(?:/\s*\d{1,4})?"  # " 02"、"- 02"、" 02 / 14"
    r"|\s*page\s*\d{1,4}"  # "Page 02"
    r"|\s*第\s*\d{1,4}\s*页"  # "第 02 页"
    r")\s*$",
    re.IGNORECASE,
)

# 连字符断行：上一行以这些字符结尾、下一行以小写字母开头，才合并。
# 软连字符（U+00AD）只是排版断行点，合并时删除；普通连字符（- / ‐）是词的一部分，保守保留。
_BREAK_HYPHENS = ("-", "‐", "­")
_SOFT_HYPHEN = "­"


class PdfExtractError(Exception):
    """PDF 抽不出正文（损坏等）。"""


class NoTextLayerError(PdfExtractError):
    """扫描版 PDF：没有文字层，不做 OCR。"""


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _is_page_number(line: str) -> bool:
    return bool(_PAGE_NUMBER.match(line.strip()))


def _strip_page_number(line: str) -> str:
    """去掉行尾的页码（页脚常常是 "www.cfm.fr 02" 这种带页码的形状）。"""
    return _PAGE_NUMBER_TAIL.sub("", line).strip()


def _header_footer_lines(pages: list[list[str]]) -> tuple[set[str], set[str]]:
    """要删掉的页眉 / 页脚行（跨页重复出现，忽略行尾页码），返回 (header, footer) 两个集合。

    少于两页无法判断，返回两个空集。页眉从每页前 _EDGE_LINES 行统计，页脚从后 _EDGE_LINES 行统计，
    删除时也只在对应的页边范围内做，避免误删正文里和页眉同文的独立标题。
    """
    n = len(pages)
    if n < 2:
        return set(), set()
    header: Counter[str] = Counter()
    footer: Counter[str] = Counter()
    for lines in pages:
        for line in lines[:_EDGE_LINES]:
            header[_norm(_strip_page_number(line))] += 1
        if len(lines) > _EDGE_LINES:
            for line in lines[-_EDGE_LINES:]:
                footer[_norm(_strip_page_number(line))] += 1
    threshold = max(2, int(n * _REPEAT_RATIO))
    return (
        {line for line, count in header.items() if count >= threshold},
        {line for line, count in footer.items() if count >= threshold},
    )


def _is_hyphen_break(line: str) -> bool:
    s = line.rstrip()
    return len(s) > 1 and s.endswith(_BREAK_HYPHENS) and s[-2].isalpha()


def _word_continuation(line: str) -> bool:
    s = line.lstrip()
    return bool(s) and s[0].islower()


def _merge_hyphens(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        while _is_hyphen_break(line) and i + 1 < len(lines) and _word_continuation(lines[i + 1]):
            tail = line.rstrip()
            if tail[-1] == _SOFT_HYPHEN:
                # 软连字符只是断行点，合并时去掉
                line = tail[:-1].rstrip() + lines[i + 1].strip()
            else:
                # 普通连字符是词的一部分（trend-following 等复合词），保留
                line = tail + lines[i + 1].strip()
            i += 1
        out.append(line)
        i += 1
    return out


def _split_long(paragraphs: list[str], limit: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    out: list[str] = []
    for para in paragraphs:
        if len(para) <= limit:
            out.append(para)
            continue
        chunk = ""
        for part in re.split(r"(?<=[.!?])\s+", para):
            if chunk and len(chunk) + len(part) + 1 > limit:
                out.append(chunk.strip())
                chunk = part
            else:
                chunk = f"{chunk} {part}".strip()
        if chunk.strip():
            out.append(chunk.strip())
    return out


def extract_pdf_text(pdf_bytes: bytes, noise_patterns: Sequence[re.Pattern[str]] = ()) -> list[str]:
    """PDF 字节 → 段落文本列表。失败抛 PdfExtractError / NoTextLayerError。

    noise_patterns：站点配置的 PDF 噪音行（封面联系方式等）；整行命中就删，不进正文。
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        raw_pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as e:  # pypdf 对损坏 PDF 抛各种异常，这里统一包一层
        raise PdfExtractError(f"PDF 解析失败（{type(e).__name__}: {e}）") from e
    if not raw_pages or not any(p.strip() for p in raw_pages):
        raise NoTextLayerError("扫描版 PDF：没有文字层，不做 OCR")

    pages = [p.splitlines() for p in raw_pages]
    nonblank = [[ln for ln in page if ln.strip()] for page in pages]
    header_drop, footer_drop = _header_footer_lines(nonblank)

    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            text = " ".join(_merge_hyphens(current)).strip()
            if text:
                paragraphs.append(text)
            current.clear()

    for page in pages:
        n_lines = len(page)
        for idx, raw_line in enumerate(page):
            if not raw_line.strip():
                flush()
                continue
            line = raw_line.strip()
            if any(p.search(line) for p in noise_patterns):
                continue  # 站点配置的噪音行（封面联系方式等），整行删掉
            norm = _norm(_strip_page_number(line))
            is_header = idx < _EDGE_LINES
            is_footer = n_lines > _EDGE_LINES and idx >= n_lines - _EDGE_LINES
            if (is_header and norm in header_drop) or (is_footer and norm in footer_drop):
                continue  # 只在页边删页眉 / 页脚，正文里同样的标题保留
            if _is_page_number(line):
                continue
            current.append(line)
        flush()  # 翻页也算一段，避免把相邻页拼成一段

    return _split_long(paragraphs)
