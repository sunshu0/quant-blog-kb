"""translate 阶段：翻译记忆 → 分批翻译未命中的段落 → 结构校验 → 推进状态。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from qbkb import store
from qbkb.models import QuotaExceededError, Segment, Status, Translator
from qbkb.translate.base import (
    MAX_TEXTS_PER_REQUEST,
    SupportsContext,
    chunk_texts,
    count_chars,
)
from qbkb.translate.glossary import Glossary
from qbkb.translate.structure import plain_text, same_structure

# 每批送出的字符上限。每批翻完立刻写进翻译记忆，所以批越小，额度中途用完时损失越少。
MAX_BATCH_CHARS = 10_000
# 每侧最多取多少字符的前后文作为 context
CONTEXT_CHARS = 600


@dataclass(slots=True)
class TranslateStats:
    url: str
    segments: int = 0
    tm_hits: int = 0  # 翻译记忆命中的段落
    translated: int = 0  # 这次新翻的段落（重复段落只算一次）
    duplicates: int = 0  # 文章内重复、复用同一份译文的段落
    chars: int = 0  # 这次送出去的字符数（含重翻），用于估算额度
    retried: int = 0  # 结构校验没过、重翻过的段落
    structure_failures: list[int] = field(default_factory=list)  # 重翻后仍不一致的段落 idx

    @property
    def complete(self) -> bool:
        return self.tm_hits + self.translated + self.duplicates == self.segments


class TranslationStopped(QuotaExceededError):
    """额度中途用完。已翻的段落已经在翻译记忆里，下次运行会接着翻。"""

    def __init__(self, stats: TranslateStats) -> None:
        super().__init__(f"额度用完，{stats.url} 翻了 {stats.translated} 段后停下")
        self.stats = stats


def _call(translator: Translator, texts: list[str], glossary: Glossary, context: str) -> list[str]:
    if context and isinstance(translator, SupportsContext):
        out = translator.translate_with_context(texts, glossary, context)
    else:
        out = translator.translate(texts, glossary)
    if len(out) != len(texts):
        raise RuntimeError(f"{translator.name} 返回 {len(out)} 条译文，送出 {len(texts)} 条")
    return out


def _context(segments: Sequence[Segment], first: int, last: int, limit: int) -> str:
    """位置 first..last 这一批的前后文：前面段落的结尾 + 后面段落的开头（纯文本）。"""
    if limit <= 0:
        return ""
    before = " ".join(plain_text(s.html) for s in segments[max(0, first - 3) : first])[-limit:]
    after = " ".join(plain_text(s.html) for s in segments[last + 1 : last + 4])[:limit]
    return "\n".join(p for p in (before.strip(), after.strip()) if p)


def _batches(positions: list[int], segments: Sequence[Segment], max_chars: int) -> list[list[int]]:
    """把待翻段落（按文章中的位置）切批：只把相邻段落放进一批，这样共用的 context 才对得上。"""
    runs: list[list[int]] = []
    for pos in positions:
        if runs and runs[-1][-1] == pos - 1:
            runs[-1].append(pos)
        else:
            runs.append([pos])
    out: list[list[int]] = []
    for run in runs:
        texts = [segments[p].html for p in run]
        out.extend(
            [run[i] for i in chunk]
            for chunk in chunk_texts(texts, MAX_TEXTS_PER_REQUEST, max_chars)
        )
    return out


def translate_article(
    conn: sqlite3.Connection,
    url: str,
    translator: Translator,
    glossary: Glossary,
    *,
    max_batch_chars: int = MAX_BATCH_CHARS,
    context_chars: int = CONTEXT_CHARS,
) -> TranslateStats:
    """翻译一篇文章的全部段落，完成后把状态推进到 translated。

    额度用完时抛 TranslationStopped（QuotaExceededError 的子类，带着统计），状态不变；
    上层应停止本轮，下次重跑会从翻译记忆接着翻。

    结构校验：译文的标签、链接、图片和原文不一致就单独重翻一次；仍不一致的译文照样存进
    翻译记忆（额度已经花了），段落 idx 记进 stats.structure_failures 和文章的 error 列，留给人看。
    翻译记忆命中的段落也会校验，所以这个记录每次运行都会重新算出来。
    """
    segments = store.get_segments(conn, url)
    if not segments:
        raise ValueError(f"{url} 没有段落，先跑 extract")
    backend, version = translator.name, glossary.tm_version
    stats = TranslateStats(url=url, segments=len(segments))

    done: dict[str, str] = {}  # src_hash → 译文
    failed_hashes: set[str] = set()
    pending: list[int] = []  # 需要翻译的段落位置（每个 hash 只取第一次出现）
    pending_hashes: set[str] = set()
    for pos, seg in enumerate(segments):
        h = seg.src_hash
        if h in done or h in pending_hashes:
            stats.duplicates += 1
            continue
        hit = store.tm_get(conn, h, backend, version)
        if hit is None:
            pending.append(pos)
            pending_hashes.add(h)
            continue
        stats.tm_hits += 1
        done[h] = hit
        if not same_structure(seg.html, hit):
            failed_hashes.add(h)

    def save(seg: Segment, tgt: str) -> None:
        store.tm_put(conn, seg.src_hash, backend, seg.html, tgt, version)
        done[seg.src_hash] = tgt
        stats.translated += 1

    try:
        for batch in _batches(pending, segments, max_batch_chars):
            texts = [segments[p].html for p in batch]
            ctx = _context(segments, batch[0], batch[-1], context_chars)
            out = _call(translator, texts, glossary, ctx)
            stats.chars += count_chars(texts)
            bad: list[int] = []
            for pos, tgt in zip(batch, out, strict=True):
                if same_structure(segments[pos].html, tgt):
                    save(segments[pos], tgt)
                else:
                    bad.append(pos)
            for pos in bad:
                seg = segments[pos]
                stats.retried += 1
                (tgt,) = _call(
                    translator, [seg.html], glossary, _context(segments, pos, pos, context_chars)
                )
                stats.chars += len(seg.html)
                if not same_structure(seg.html, tgt):
                    failed_hashes.add(seg.src_hash)
                save(seg, tgt)
    except QuotaExceededError as e:
        raise TranslationStopped(stats) from e

    stats.structure_failures = [s.idx for s in segments if s.src_hash in failed_hashes]
    error = None
    if stats.structure_failures:
        error = "结构校验未通过的段落：" + ", ".join(map(str, stats.structure_failures))
    store.set_status(
        conn,
        url,
        Status.TRANSLATED,
        translator=backend,
        translated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        error=error,
    )
    return stats


def estimate_chars(
    conn: sqlite3.Connection, urls: Iterable[str], backend: str, glossary: Glossary
) -> int:
    """估算翻完这些文章还要多少字符（只算翻译记忆未命中的段落，跨文章去重）。

    和 base.FREE_MONTHLY_CHARS 比较，就知道这个月的额度够不够。
    """
    seen: set[str] = set()
    total = 0
    for url in urls:
        for seg in store.get_segments(conn, url):
            h = seg.src_hash
            if h in seen:
                continue
            seen.add(h)
            if store.tm_get(conn, h, backend, glossary.tm_version) is None:
                total += len(seg.html)
    return total
