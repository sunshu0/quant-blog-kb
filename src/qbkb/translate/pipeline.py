"""translate 阶段：翻译记忆 → 分批翻译未命中的段落 → 结构校验 → 推进状态。"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from qbkb import store
from qbkb.models import QuotaExceededError, Segment, Status, Translator
from qbkb.translate.base import (
    MAX_REQUEST_CHARS,
    MAX_TEXTS_PER_REQUEST,
    TranslatorUnavailableError,
    chunk_texts,
    count_chars,
)
from qbkb.translate.glossary import Glossary
from qbkb.translate.structure import plain_text, same_structure

# 一批请求的 context 总长上限（字符）。DeepL 不对 context 计费，但它占请求体。
CONTEXT_CHARS = 1_200


@dataclass(slots=True)
class TranslateStats:
    url: str
    segments: int = 0
    tm_hits: int = 0  # 翻译记忆命中（且结构校验通过）的段落
    translated: int = 0  # 这次新翻、通过校验的段落（重复段落只算一次）
    duplicates: int = 0  # 文章内重复、复用同一份译文的段落
    chars: int = 0  # 这次送出去的字符数（含重翻），用于估算额度
    retried: int = 0  # 结构校验没过、重翻过的段落
    rejected: list[int] = field(default_factory=list)  # 两次都没过校验、需人工复查的段落 idx
    oversized: list[int] = field(default_factory=list)  # 超过单次请求上限、没送出去的段落 idx

    @property
    def needs_review(self) -> bool:
        return bool(self.rejected or self.oversized)


class TranslationStopped(Exception):
    """翻译中途停下。已经通过校验的段落都在翻译记忆里，下次运行会接着翻、不重复计费。"""

    def __init__(self, stats: TranslateStats, reason: str) -> None:
        super().__init__(f"{stats.url} 翻了 {stats.translated} 段后停下：{reason}")
        self.stats = stats


class QuotaStopped(TranslationStopped, QuotaExceededError):
    """额度用完。上层应停止本轮，等额度恢复。"""


class BackendStopped(TranslationStopped):
    """后端暂时不可用（限流 / 网络 / 5xx 重试用尽）。上层应停止本轮，稍后再跑。"""


def needs_review(row: sqlite3.Row) -> bool:
    """文章是否在等人工复查（区别于真正的失败）。"""
    return row["status"] == Status.NEEDS_REVIEW


def _call(translator: Translator, texts: list[str], glossary: Glossary, context: str) -> list[str]:
    out = translator.translate(texts, glossary, context)
    if len(out) != len(texts):
        raise RuntimeError(f"{translator.name} 返回 {len(out)} 条译文，送出 {len(texts)} 条")
    return out


def _context(segments: Sequence[Segment], batch: list[int], limit: int) -> str:
    """一批段落的参考上下文：批里每段在原文中的前一段和后一段（不在本批里的），纯文本。

    批可以由不相邻的段落组成，所以按每段各自的邻居取，再平均分配 limit 个字符。
    前一段取结尾，后一段取开头。
    """
    if limit <= 0:
        return ""
    members = set(batch)
    before = {p - 1 for p in batch if p > 0 and p - 1 not in members}
    after = {p + 1 for p in batch if p + 1 < len(segments) and p + 1 not in members}
    neighbours = sorted(before | after)
    if not neighbours:
        return ""
    per = max(60, limit // len(neighbours))
    pieces = []
    for p in neighbours:
        text = plain_text(segments[p].html)
        pieces.append(text[:per] if p in after else text[-per:])
    return "\n".join(t for t in pieces if t)[:limit]


def translate_article(
    conn: sqlite3.Connection,
    url: str,
    translator: Translator,
    glossary: Glossary,
    *,
    retry_rejected: bool = False,
    max_batch_chars: int = MAX_REQUEST_CHARS,
    context_chars: int = CONTEXT_CHARS,
) -> TranslateStats:
    """翻译一篇文章的全部段落。

    - 查翻译记忆（按段落的术语版本），只翻未命中的；文章内重复的段落只翻一次。
      未命中的段落不管是否相邻都合进同一批（每批不超过 max_batch_chars 和 50 条）。
    - 每批翻完，通过结构校验的段落立刻写进翻译记忆。没通过的单独再翻一次，
      而且不带 context（同样的输入大概率得到同样的结果）。
    - 还是没通过：译文以 quality = rejected 存进翻译记忆，tm_get 不会返回它；
      以后的运行也不再自动重翻它（不会每次都花额度），除非 retry_rejected=True。
    - 单段超过 max_batch_chars 的不送出，记为 oversized，其余段落照常翻。
    - 结果：全部通过 → translated；有 rejected / oversized → needs_review，
      error 列写明是哪些段落，等人工复查。
    - 额度用完抛 QuotaStopped，后端重试用尽抛 BackendStopped（都是 TranslationStopped，
      带 stats），文章状态不变。
    """
    segments = store.get_segments(conn, url)
    if not segments:
        raise ValueError(f"{url} 没有段落，先跑 extract")
    backend = translator.name
    stats = TranslateStats(url=url, segments=len(segments))

    rejected: set[str] = set()
    oversized: set[str] = set()
    pending: list[int] = []  # 要送去翻译的段落位置（每个 hash 只取第一次出现）
    versions: dict[int, str] = {}
    seen: set[str] = set()
    for pos, seg in enumerate(segments):
        h = seg.src_hash
        if h in seen:
            stats.duplicates += 1
            continue
        seen.add(h)
        version = versions[pos] = glossary.version_for(seg.html)
        entry = store.tm_lookup(conn, h, backend, version)
        if (
            entry is not None
            and entry.quality == store.TM_OK
            and same_structure(seg.html, entry.tgt_html)
        ):
            stats.tm_hits += 1
            continue
        if not retry_rejected and entry is not None:
            # 以前就没通过校验（rejected，或合格译文按现在的校验规则不过）：
            # 不自动重翻，继续等人工复查
            rejected.add(h)
            continue
        if len(seg.html) > max_batch_chars:
            oversized.add(h)
            continue
        pending.append(pos)

    def save(pos: int, tgt: str) -> None:
        seg = segments[pos]
        store.tm_put(conn, seg.src_hash, backend, seg.html, tgt, versions[pos])
        rejected.discard(seg.src_hash)
        stats.translated += 1

    def finish_lists() -> None:
        stats.rejected = [s.idx for s in segments if s.src_hash in rejected]
        stats.oversized = [s.idx for s in segments if s.src_hash in oversized]

    texts = [segments[p].html for p in pending]
    try:
        for chunk in chunk_texts(texts, MAX_TEXTS_PER_REQUEST, max_batch_chars):
            batch = [pending[i] for i in chunk]
            batch_texts = [texts[i] for i in chunk]
            out = _call(translator, batch_texts, glossary, _context(segments, batch, context_chars))
            stats.chars += count_chars(batch_texts)
            bad: list[int] = []
            # 先把好段落存下来，再重翻坏的：重翻时额度用完，已计费的好段落也不会丢
            for pos, tgt in zip(batch, out, strict=True):
                if same_structure(segments[pos].html, tgt):
                    save(pos, tgt)
                else:
                    bad.append(pos)
            for pos in bad:
                seg = segments[pos]
                stats.retried += 1
                (tgt,) = _call(translator, [seg.html], glossary, "")
                stats.chars += len(seg.html)
                if same_structure(seg.html, tgt):
                    save(pos, tgt)
                else:
                    store.tm_put(
                        conn,
                        seg.src_hash,
                        backend,
                        seg.html,
                        tgt,
                        versions[pos],
                        quality=store.TM_REJECTED,
                    )
                    rejected.add(seg.src_hash)
    except QuotaExceededError as e:
        finish_lists()
        raise QuotaStopped(stats, f"额度用完（{e}）") from e
    except TranslatorUnavailableError as e:
        finish_lists()
        raise BackendStopped(stats, str(e)) from e

    finish_lists()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    if stats.needs_review:
        parts = []
        if stats.rejected:
            parts.append("结构校验未通过的段落 " + ", ".join(map(str, stats.rejected)))
        if stats.oversized:
            parts.append(
                f"超过单次请求上限（{max_batch_chars} 字符）的段落 "
                + ", ".join(map(str, stats.oversized))
            )
        error = "；".join(parts)
        store.set_status(
            conn, url, Status.NEEDS_REVIEW, translator=backend, translated_at=now, error=error
        )
    else:
        store.set_status(
            conn, url, Status.TRANSLATED, translator=backend, translated_at=now, error=None
        )
    return stats


def _unique_segments(conn: sqlite3.Connection, urls: Iterable[str]) -> list[Segment]:
    seen: set[str] = set()
    out: list[Segment] = []
    for url in urls:
        for seg in store.get_segments(conn, url):
            if seg.src_hash not in seen:
                seen.add(seg.src_hash)
                out.append(seg)
    return out


def estimate_chars(
    conn: sqlite3.Connection, urls: Iterable[str], backend: str, glossary: Glossary
) -> int:
    """估算翻完这些文章还要多少字符（只算翻译记忆未命中的段落，跨文章去重）。

    和 base.FREE_MONTHLY_CHARS 比较，就知道这个月的额度够不够。
    """
    return sum(
        len(seg.html)
        for seg in _unique_segments(conn, urls)
        if store.tm_get(conn, seg.src_hash, backend, glossary.version_for(seg.html)) is None
    )


def glossary_upgrade_cost(
    conn: sqlite3.Connection,
    urls: Iterable[str],
    backend: str,
    old: Glossary,
    new: Glossary,
) -> int:
    """术语表从 old 换成 new，会让多少字符的已翻段落重翻（换之前先看一眼）。"""
    total = 0
    for seg in _unique_segments(conn, urls):
        before, after = old.version_for(seg.html), new.version_for(seg.html)
        if before != after and store.tm_get(conn, seg.src_hash, backend, before) is not None:
            total += len(seg.html)
    return total
