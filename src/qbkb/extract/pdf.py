"""PDF 正文抽取：pypdf 取文字 → 段落。

只用 pypdf（BSD-3-Clause，见 design.md 4.3）按页取纯文本，不去解析排版结构：
- 页眉 / 页脚 / 页码：跨页重复出现的行、以及纯页码行去掉；
- 连字符断行合并（"incre-\nmental" → "incremental"）；
- 空行切段、翻页也算一段，过长的段落按句号切分；
- 公式 / 表格没有可靠的结构化提取，保留 pypdf 给的原文字，由调用方在 qc_issues 里记提示；
- 扫描版（没有文字层）抛 NoTextLayerError，不做 OCR。
"""

from __future__ import annotations

import io
import re
from collections import Counter

from pypdf import PdfReader

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

# 连字符断行：上一行以这些字符结尾、下一行以小写字母开头，才合并
_BREAK_HYPHENS = ("-", "‐", "­")


class PdfExtractError(Exception):
    """PDF 抽不出正文（损坏等）。"""


class NoTextLayerError(PdfExtractError):
    """扫描版 PDF：没有文字层，不做 OCR。"""


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _is_page_number(line: str) -> bool:
    return bool(_PAGE_NUMBER.match(line.strip()))


def _header_footer_lines(pages: list[list[str]]) -> set[str]:
    """要删掉的页眉 / 页脚（跨页重复出现的行）。少于两页无法判断，返回空集。"""
    n = len(pages)
    if n < 2:
        return set()
    header: Counter[str] = Counter()
    footer: Counter[str] = Counter()
    for lines in pages:
        for line in lines[:_EDGE_LINES]:
            header[_norm(line)] += 1
        if len(lines) > _EDGE_LINES:
            for line in lines[-_EDGE_LINES:]:
                footer[_norm(line)] += 1
    threshold = max(2, int(n * _REPEAT_RATIO))
    return {line for line, count in header.items() if count >= threshold} | {
        line for line, count in footer.items() if count >= threshold
    }


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
            line = line[:-1].rstrip() + lines[i + 1].strip()
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


def extract_pdf_text(pdf_bytes: bytes) -> list[str]:
    """PDF 字节 → 段落文本列表。失败抛 PdfExtractError / NoTextLayerError。"""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        raw_pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as e:  # pypdf 对损坏 PDF 抛各种异常，这里统一包一层
        raise PdfExtractError(f"PDF 解析失败（{type(e).__name__}: {e}）") from e
    if not raw_pages or not any(p.strip() for p in raw_pages):
        raise NoTextLayerError("扫描版 PDF：没有文字层，不做 OCR")

    pages = [p.splitlines() for p in raw_pages]
    nonblank = [[ln for ln in page if ln.strip()] for page in pages]
    drop = _header_footer_lines(nonblank)

    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            text = " ".join(_merge_hyphens(current)).strip()
            if text:
                paragraphs.append(text)
            current.clear()

    for page in pages:
        for raw_line in page:
            if not raw_line.strip():
                flush()
                continue
            line = raw_line.strip()
            if _norm(line) in drop or _is_page_number(line):
                continue
            current.append(line)
        flush()  # 翻页也算一段，避免把相邻页拼成一段

    return _split_long(paragraphs)
