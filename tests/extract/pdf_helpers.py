"""测试用：构造最小合法 PDF（有文字层），不联网、不依赖额外库。

用纯字节拼 PDF（pypdf 只负责读，不负责写文本）。每页从上到下按行画文字，这样
extract_pdf_text 能拿到确定性的行序，方便测页眉 / 页脚 / 页码 / 连字符断行。
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _content_stream(text: str, font_size: float, leading: float) -> bytes:
    parts: list[bytes] = [
        b"BT",
        f"/F1 {font_size} Tf".encode(),
        f"{leading} TL".encode(),
        b"72 700 Td",
    ]
    for line in text.split("\n"):
        parts.append(f"({_escape(line)}) Tj T*".encode())
    parts.append(b"ET")
    return b"\n".join(parts)


def make_pdf(pages: list[str], font_size: float = 12.0, leading: float = 16.0) -> bytes:
    """每页一段文字（`\n` 分隔的行），生成一个最小 PDF 的字节。"""
    n = len(pages)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
    for i, page_text in enumerate(pages):
        page_num = 4 + 2 * i
        content_num = 5 + 2 * i
        content = _content_stream(page_text, font_size, leading)
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>"
        ).encode()
        objects[content_num] = (
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode()
        out += objects[num]
        out += b"\nendobj\n"
    xref_pos = len(out)
    total = max(objects) + 1
    out += f"xref\n0 {total}\n".encode()
    out += b"0000000000 65535 f \n"
    for num in range(1, total):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {total} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode()
    return bytes(out)
