"""控制台「栏目抽样」页面的纯逻辑：摘要提取、标题 / 摘要翻译、抽样表标记读写。

数据源和 CLI 一致：抽样表就是 `output/sections/<站点>.csv`（沿用 `sections.survey` 的列和
UTF-8 带 BOM 的写法），网页读写的和 Excel / `qbkb sections` 用的是同一个文件，两边的标记互通。

翻译缓存复用 `translation_memory`，理由：标题和摘要都只是「另一个翻译单元」（标题包成
`<p>` 后和 `qbkb translate` 用的是同一个 `title_unit`，摘要是 600 字符的纯文本也包成 `<p>`），
各自有独立的 src_hash，不会和正文段落 / 标题的译文互相污染；命中翻译记忆就不重复计费。
"""

from __future__ import annotations

import contextlib
import csv
import html
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

from qbkb import store
from qbkb.models import Translator, segment_hash
from qbkb.sections import survey
from qbkb.translate.glossary import Glossary
from qbkb.translate.pipeline import lookup_translation
from qbkb.translate.structure import plain_text, same_structure

# 摘要长度：正文前约 600 个字符（纯文本）
SUMMARY_CHARS = 600


def summary_unit(text: str) -> str:
    """摘要的翻译单元：和标题一样包成一个 <p>，走同一套翻译记忆。"""
    return f"<p>{html.escape(text, quote=False)}</p>"


def summary_text(conn: sqlite3.Connection, url: str) -> str:
    """正文前约 SUMMARY_CHARS 个字符（去掉标签、折叠空白的纯文本）。"""
    pieces: list[str] = []
    total = 0
    for seg in store.get_segments(conn, url):
        text = plain_text(seg.html)
        if text:
            pieces.append(text)
            total += len(text)
        if total >= SUMMARY_CHARS:
            break
    return " ".join(pieces)[:SUMMARY_CHARS]


def missing_units(
    conn: sqlite3.Connection, units: Sequence[str], backend: str, glossary: Glossary
) -> list[str]:
    """还没在翻译记忆里（或没有固定译文）的单位，去重、保持顺序。"""
    return [
        u for u in dict.fromkeys(units) if lookup_translation(conn, u, backend, glossary) is None
    ]


def estimate_units(
    conn: sqlite3.Connection, units: Sequence[str], backend: str, glossary: Glossary
) -> int:
    """估算翻这些单位还要多少字符（只算未命中的，按送出长度算，是上界）。"""
    return sum(len(u) for u in missing_units(conn, units, backend, glossary))


def translate_units(
    conn: sqlite3.Connection,
    units: Sequence[str],
    translator: Translator,
    glossary: Glossary,
) -> int:
    """翻译一批单位（标题 / 摘要），命中的跳过，新译且结构校验通过的写进翻译记忆。

    返回这次新译了几条。额度用完 / 后端不可用的异常原样往上抛，由调用方转成提示。
    """
    pending = missing_units(conn, units, translator.name, glossary)
    if not pending:
        return 0
    out = translator.translate(pending, glossary, context="")
    if len(out) != len(pending):
        raise RuntimeError(f"{translator.name} 返回 {len(out)} 条译文，送出 {len(pending)} 条")
    done = 0
    for unit, tgt in zip(pending, out, strict=True):
        if same_structure(unit, tgt):
            store.tm_put(
                conn,
                segment_hash(unit),
                translator.name,
                unit,
                tgt,
                glossary.version_for(unit),
            )
            done += 1
    return done


def translated_summary(
    conn: sqlite3.Connection, summary: str, backend: str, glossary: Glossary
) -> str | None:
    """摘要的中文（纯文本）；翻译记忆里没有就返回 None。"""
    if not summary:
        return None
    tgt = lookup_translation(conn, summary_unit(summary), backend, glossary)
    return plain_text(tgt) if tgt else None


class SamplingCsvError(OSError):
    """抽样表读不出来 / 写不回去（比如被 Excel 占用）。message 是给用户看的中文。"""


def update_marks(
    path: Path, marks: Mapping[str, tuple[str | None, str | None]]
) -> list[dict[str, str]]:
    """把「要不要 / 备注」写回抽样表，保持 UTF-8 BOM 和原列顺序。返回更新后的行。

    每个网址给 (要不要, 备注)，为 None 的那一列不动（只改要改的那一列，互不覆盖）。
    网址不在抽样表里抛 KeyError；文件读不出来 / 写不进去抛 SamplingCsvError（不丢已有标记）。
    """
    try:
        rows = survey.read_csv(path)
    except OSError as e:
        raise SamplingCsvError(
            f"抽样表打不开，可能被别的程序（比如 Excel）占用了，没改任何标记：{e}"
        ) from e
    index = {r.get("网址", ""): i for i, r in enumerate(rows)}
    for url, (mark, note) in marks.items():
        if url not in index:
            raise KeyError(url)
        i = index[url]
        if mark is not None:
            rows[i]["要不要"] = mark
        if note is not None:
            rows[i]["备注"] = note
    _write_csv(path, rows)
    return rows


def _write_csv(path: Path, rows: Sequence[dict[str, str]]) -> None:
    """先写临时文件再原子替换；写不进去时清理临时文件、抛 SamplingCsvError（原文不动）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(survey.COLUMNS)
            for r in rows:
                w.writerow([r.get(c, "") for c in survey.COLUMNS])
        tmp.replace(path)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise SamplingCsvError(
            f"抽样表写不回去，可能被别的程序（比如 Excel）占用了。这次没有保存，"
            f"已有的标记都还在：{e}"
        ) from e
