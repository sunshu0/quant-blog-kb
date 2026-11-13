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
import os
import sqlite3
import tempfile
import threading
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from qbkb import store
from qbkb.models import Translator, segment_hash
from qbkb.translate.base import MAX_REQUEST_CHARS, MAX_TEXTS_PER_REQUEST, chunk_texts
from qbkb.translate.glossary import Glossary
from qbkb.translate.pipeline import lookup_translation
from qbkb.translate.structure import plain_text, same_structure

# 摘要长度：正文前约 600 个字符（纯文本）
SUMMARY_CHARS = 600

# 同一张抽样表（按解析后的路径）一把进程内锁：mark / generate 的读→改→原子写都在锁内，
# 防止并发标注互相覆盖。
_csv_locks: dict[str, threading.Lock] = {}
_csv_locks_guard = threading.Lock()


def _csv_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _csv_locks_guard:
        lock = _csv_locks.get(key)
        if lock is None:
            lock = _csv_locks[key] = threading.Lock()
        return lock


@contextlib.contextmanager
def csv_lock(path: Path) -> Iterator[None]:
    """对同一张抽样表串行化读写（mark 与 generate 共用同一把锁）。"""
    with _csv_lock(path):
        yield


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
    """还没翻译过（无固定译文、翻译记忆里没有 ok 或 rejected 记录）的单位，去重、保持顺序。

    rejected 也算「翻译过」：结构校验失败的负缓存，避免每次点都重新计费重翻。
    """
    out: list[str] = []
    for u in dict.fromkeys(units):
        h = segment_hash(u)
        if h in glossary.fixed:
            continue
        if store.tm_lookup(conn, h, backend, glossary.version_for(u)) is None:
            out.append(u)
    return out


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
) -> tuple[int, list[str]]:
    """翻译一批单位（标题 / 摘要），按后端上限分批，每批成功就立即写缓存。

    - 命中的（含 rejected 负缓存）跳过；新译且结构校验通过的写进翻译记忆；
    - 结构校验不过的以 rejected 负缓存，下次不重翻，随返回值报给页面；
    - 返回 (新译成功的条数, 结构校验失败没翻成功的单元)。

    额度用完 / 后端不可用的异常原样往上抛（已成功的批次已落缓存，重试不会重复计费）。
    """
    pending = missing_units(conn, units, translator.name, glossary)
    done = 0
    failed: list[str] = []
    for chunk in chunk_texts(pending, MAX_TEXTS_PER_REQUEST, MAX_REQUEST_CHARS):
        batch = [pending[i] for i in chunk]
        out = translator.translate(batch, glossary, context="")
        if len(out) != len(batch):
            raise RuntimeError(f"{translator.name} 返回 {len(out)} 条译文，送出 {len(batch)} 条")
        for unit, tgt in zip(batch, out, strict=True):
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
            else:
                # 结构校验不过：负缓存（rejected），不重复计费，回报给页面
                store.tm_put(
                    conn,
                    segment_hash(unit),
                    translator.name,
                    unit,
                    tgt,
                    glossary.version_for(unit),
                    quality=store.TM_REJECTED,
                )
                failed.append(unit)
    return done, failed


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


class SamplingCsvFormatError(ValueError):
    """抽样表结构不对（缺必需的列等）。"""


def update_marks(
    path: Path, marks: Mapping[str, tuple[str | None, str | None]]
) -> list[dict[str, str]]:
    """把「要不要 / 备注」写回抽样表，保持 UTF-8 BOM、原列顺序和 Excel 加的多余列。

    每个网址给 (要不要, 备注)，为 None 的那一列不动（只改要改的那一列，互不覆盖）。
    网址不在抽样表里抛 KeyError；缺必需列抛 SamplingCsvFormatError；
    文件读不出来 / 写不进去抛 SamplingCsvError（不丢已有标记）。整段读改写都在锁内。
    """
    lock = _csv_lock(path)
    with lock:
        try:
            with path.open(encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                fieldnames = list(reader.fieldnames or [])
                rows = [dict(r) for r in reader]
        except OSError as e:
            raise SamplingCsvError(
                f"抽样表打不开，可能被别的程序（比如 Excel）占用了，没改任何标记：{e}"
            ) from e
        missing_cols = [c for c in ("网址", "要不要", "备注") if c not in fieldnames]
        if missing_cols:
            raise SamplingCsvFormatError(
                f"抽样表缺少必需的列：{'、'.join(missing_cols)}（请用标准抽样表，或至少保留这些列）"
            )
        index = {r.get("网址", ""): i for i, r in enumerate(rows)}
        for url, (mark, note) in marks.items():
            if url not in index:
                raise KeyError(url)
            i = index[url]
            if mark is not None:
                rows[i]["要不要"] = mark
            if note is not None:
                rows[i]["备注"] = note
        _write_csv(path, rows, fieldnames)
    return rows


def _write_csv(path: Path, rows: Sequence[dict[str, str]], fieldnames: Sequence[str]) -> None:
    """按原列顺序写回（含 Excel 加的多余列）；先写唯一临时文件再原子替换。

    写不进去时清理临时文件、抛 SamplingCsvError（原文不动）。临时文件用唯一名字，避免并发
    写同一张表时互相截断临时文件（跨进程同时写表、互相覆盖的冲突本版不处理）。
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(fieldnames))
            w.writeheader()
            w.writerows(rows)
        tmp.replace(path)
    except OSError as e:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise SamplingCsvError(
            f"抽样表写不回去，可能被别的程序（比如 Excel）占用了。这次没有保存，"
            f"已有的标记都还在：{e}"
        ) from e
