"""translate 阶段：翻译记忆 → 分批翻译未命中的段落 → 结构校验 → 推进状态。"""

from __future__ import annotations

import html
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from qbkb import store
from qbkb.models import QuotaExceededError, Segment, Status, Translator, segment_hash

if TYPE_CHECKING:
    from qbkb.sections import SectionRules
from qbkb.translate.base import (
    MAX_REQUEST_CHARS,
    MAX_TEXTS_PER_REQUEST,
    ArticleTranslationError,
    TranslatorUnavailableError,
    chunk_units,
    count_chars,
)
from qbkb.translate.fixed import FIXED_FILE, is_interview, untranslated_units
from qbkb.translate.footnotes import repair_marks, sent_length
from qbkb.translate.glossary import PREVIOUS_SEGMENT_RULES, SEGMENT_RULES, Glossary
from qbkb.translate.structure import plain_text, same_structure

# 一批请求的 context 总长上限（字符）。DeepL 不对 context 计费，但它占请求体。
CONTEXT_CHARS = 1_200


def _unit_info(translator: Translator, html: str, glossary: Mapping[str, str]) -> tuple[int, int]:
    """这一段会展开成几个翻译单元、总长多少。

    后端能报就用后端的（DeepL 把分行段落拆成一行一个单元、50 条上限按单元数算；
    Azure 把术语标记 / 分行展开后的总长一起报）；没有这个方法的后端（假后端等）退回
    「一段一个单元、长度用脚注占位展开后的」。
    """
    fn = getattr(translator, "request_units", None)
    if callable(fn):
        return cast("Callable[[str, Mapping[str, str]], tuple[int, int]]", fn)(html, glossary)
    return 1, sent_length(html)


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
    fixed: int = 0  # 用了固定译文的段落（不计费）
    speaker_labels: int = 0  # 原样保留的说话人标签（不计费）
    revalidated: int = 0  # 翻译记忆里按新规则改判合格或就地修好的段落（不计费）
    changed: int = 0  # 这次写进翻译记忆、让导出结果可能变化的段落（新译、修好、换成固定译文）

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


class FixedTranslationError(ValueError):
    """固定译文和原文结构不一致：这是配置错误（fixed_translations.yaml 写错了）。

    应该在循环开始前整体报错（退出码 1），而不是逐篇把文章标 failed。
    """


def needs_review(row: sqlite3.Row) -> bool:
    """文章是否在等人工复查（区别于真正的失败）。"""
    return row["status"] == Status.NEEDS_REVIEW


def fixed_translation_errors(
    conn: sqlite3.Connection, urls: Iterable[str], glossary: Glossary
) -> list[str]:
    """待翻译文章里命中固定译文、但译文和原文结构不一致的段落。空列表 = 全部一致。

    固定译文按原文段落的 hash 对应，所以必须先知道要翻哪些段落才能核对结构；这里在
    循环开始前扫一遍，有错就整体退出 1，而不是翻译到一半才撞上。
    """
    problems: list[str] = []
    seen: set[str] = set()
    for url in urls:
        for seg in store.get_segments(conn, url):
            h = seg.src_hash
            if h in seen:
                continue
            seen.add(h)
            tgt = glossary.fixed.get(h)
            if tgt is not None and not same_structure(seg.html, tgt):
                problems.append(f"固定译文 {h[:12]}…（{url}）和原文结构不一致")
    return problems


def title_unit(title: str) -> str:
    """标题作为一个额外的翻译单元：包成一个 <p>，和正文段落一样走翻译记忆。"""
    return f"<p>{html.escape(' '.join(title.split()), quote=False)}</p>"


def lookup_translation(
    conn: sqlite3.Connection, src_html: str, backend: str, glossary: Glossary
) -> str | None:
    """按 translate 的规则查一段的合格译文（backend + 这段的术语版本）。export 也用它。

    有固定译文的段落直接返回固定译文（翻译记忆里可能还是以前 DeepL 的版本）。
    """
    h = segment_hash(src_html)
    if h in glossary.fixed:
        return glossary.fixed[h]
    return store.tm_get(conn, h, backend, glossary.version_for(src_html))


def translated_title(
    conn: sqlite3.Connection, title: str | None, backend: str, glossary: Glossary
) -> str | None:
    """标题的中文（纯文本）；翻译记忆里没有就返回 None。"""
    if not title or not title.strip():
        return None
    tgt = lookup_translation(conn, title_unit(title), backend, glossary)
    if tgt is None:
        return None
    return plain_text(tgt) or None


def _translate_title(
    conn: sqlite3.Connection,
    title: str | None,
    translator: Translator,
    glossary: Glossary,
    context: str,
    stats: TranslateStats,
) -> str | None:
    """翻译标题（命中翻译记忆就不花额度）。结构校验没过就放弃标题、不影响正文。"""
    if not title or not title.strip():
        return None
    unit = title_unit(title)
    found = translated_title(conn, title, translator.name, glossary)
    if found is not None:
        return found
    (tgt,) = _call(translator, [unit], glossary, context)
    stats.chars += len(unit)
    if not same_structure(unit, tgt):
        return None
    store.tm_put(conn, segment_hash(unit), translator.name, unit, tgt, glossary.version_for(unit))
    return plain_text(tgt) or None


def _call(translator: Translator, texts: list[str], glossary: Glossary, context: str) -> list[str]:
    out = translator.translate(texts, glossary, context)
    if len(out) != len(texts):
        raise ArticleTranslationError(
            f"{translator.name} 返回 {len(out)} 条译文，送出 {len(texts)} 条"
        )
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


def _batch_length(translator: Translator, segment: str, glossary: Glossary) -> int:
    """pipeline 分批用：这一段实际送出去的总长度（后端加词典标记/脚注占位/行拆分后求和）。

    Azure 用 sent_total（含动态词典标记展开后的长度），DeepL 用 footnotes.sent_length
    （只含脚注占位展开）。pipeline 按它累加才能保证一批只对应一个请求。
    """
    fn = getattr(translator, "sent_total", None)
    if callable(fn):
        return cast(int, fn(segment, glossary))
    return sent_length(segment)


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
      未命中的段落不管是否相邻都合进同一批（每批不超过 max_batch_chars 和 50 个翻译单元）。
    - 每批翻完，通过结构校验的段落立刻写进翻译记忆。没通过的单独再翻一次，
      而且不带 context（同样的输入大概率得到同样的结果）。
    - 还是没通过：译文以 quality = rejected 存进翻译记忆，tm_get 不会返回它；
      以后的运行也不再自动重翻它（不会每次都花额度），除非 retry_rejected=True。
    - 单段展开后超过单次请求上限（字符或单元数）的不送出，记为 oversized，其余段落照常翻。
    - 结果：全部通过 → translated；有 rejected / oversized → needs_review，
      error 列写明是哪些段落，等人工复查。
    - 已经 translated / published 的文章（translate --recheck）：没有任何段落变化时保持原状态和
      translated_at，不会把已发布的文章降回 translated；有段落新译、修好或换成固定译文的，
      回到 translated，下次 export 重新生成。
    - 额度用完抛 QuotaStopped，后端重试用尽抛 BackendStopped（都是 TranslationStopped，
      带 stats），文章状态不变。
    - 标题（articles.title）作为额外的一个翻译单元（title_unit）在正文之后翻译，同样走
      翻译记忆，结果写进 title_zh；标题结构校验没过只是 title_zh 留空，不影响文章状态。
    """
    segments = store.get_segments(conn, url)
    if not segments:
        raise ValueError(f"{url} 没有段落，先跑 extract")
    backend = translator.name
    stats = TranslateStats(url=url, segments=len(segments))
    before = store.get_article(conn, url)

    rejected: set[str] = set()
    oversized: set[str] = set()
    pending: list[int] = []  # 要送去翻译的段落位置（每个 hash 只取第一次出现）
    versions: dict[int, str] = {}
    seen: set[str] = set()
    interview = before is not None and is_interview(before["tags"])
    fixed_units = untranslated_units(segments, glossary.fixed, interview=interview)
    # 单次请求的字符上限：后端可能比 pipeline 更保守（Azure 的限速窗口），取较小的
    request_limit = min(max_batch_chars, getattr(translator, "request_limit", max_batch_chars))
    for pos, seg in enumerate(segments):
        h = seg.src_hash
        if h in seen:
            stats.duplicates += 1
            continue
        seen.add(h)
        version = versions[pos] = glossary.version_for(seg.html)
        entry = store.tm_lookup(conn, h, backend, version)
        if h in fixed_units:
            # 固定译文 / 说话人标签：不送翻译、不计费，写进翻译记忆给 export 用
            tgt, kind = fixed_units[h]
            if not same_structure(seg.html, tgt):
                raise FixedTranslationError(
                    f"固定译文 {h[:12]}… 和原文结构不一致，请检查 {FIXED_FILE}"
                )
            if entry is None or entry.quality != store.TM_OK or entry.tgt_html != tgt:
                store.tm_put(conn, h, backend, seg.html, tgt, version)
                stats.changed += 1
            if kind == "fixed":
                stats.fixed += 1
            else:
                stats.speaker_labels += 1
            continue
        if entry is not None:
            # 按现在的校验规则重新判断：D7 放宽后以前 rejected 的可能已经合格；
            # 以前算 ok 的可能有 DeepL 自己写的 Unicode 上标，能确定修好的就地修掉（不花额度）
            tgt = entry.tgt_html
            if not same_structure(seg.html, tgt):
                tgt = repair_marks(seg.html, tgt)
            if same_structure(seg.html, tgt):
                if entry.quality != store.TM_OK or tgt != entry.tgt_html:
                    store.tm_put(conn, h, backend, seg.html, tgt, version)
                    stats.revalidated += 1
                    stats.changed += 1
                stats.tm_hits += 1
                continue
        if not retry_rejected and entry is not None:
            # 以前就没通过校验（rejected，或合格译文按现在的校验规则不过）：
            # 不自动重翻，继续等人工复查
            rejected.add(h)
            continue
        n_units, unit_chars = _unit_info(translator, seg.html, glossary)
        if n_units > MAX_TEXTS_PER_REQUEST or unit_chars > request_limit:
            oversized.add(h)
            continue
        pending.append(pos)

    def save(pos: int, tgt: str) -> None:
        seg = segments[pos]
        store.tm_put(conn, seg.src_hash, backend, seg.html, tgt, versions[pos])
        rejected.discard(seg.src_hash)
        stats.translated += 1
        stats.changed += 1

    def finish_lists() -> None:
        stats.rejected = [s.idx for s in segments if s.src_hash in rejected]
        stats.oversized = [s.idx for s in segments if s.src_hash in oversized]

    texts = [segments[p].html for p in pending]
    # 按"实际送出去的单元数和长度"（分行 + 脚注占位 / 术语标记展开后）分批，而不是原文段落数/长度：
    # 保证一批只对应后端的一个请求，后一块失败时前面已计费的译文已经逐批落进翻译记忆、不会整个丢掉
    info = [_unit_info(translator, t, glossary) for t in texts]
    counts = [n for n, _ in info]
    lengths = [n for _, n in info]
    try:
        for chunk in chunk_units(counts, lengths, MAX_TEXTS_PER_REQUEST, request_limit):
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
        row = store.get_article(conn, url)
        title = None if row is None else row["title"]
        context = plain_text(segments[0].html)[:context_chars] if context_chars > 0 else ""
        title_zh = _translate_title(conn, title, translator, glossary, context, stats)
    except QuotaExceededError as e:
        finish_lists()
        raise QuotaStopped(stats, f"额度用完（{e}）") from e
    except TranslatorUnavailableError as e:
        finish_lists()
        raise BackendStopped(stats, str(e)) from e

    finish_lists()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    prior = None if before is None else Status(before["status"])
    if before is not None and title_zh != before["title_zh"]:
        stats.changed += 1
    if not stats.changed and before is not None and before["translated_at"]:
        # 什么都没变（recheck 时最常见）：不刷新翻译时间
        now = str(before["translated_at"])
    if (
        not stats.needs_review
        and not stats.changed
        and prior in (Status.TRANSLATED, Status.PUBLISHED)
        and before is not None
        and before["translator"]
    ):
        # 只有「已经翻过全文」的文章才保持原状态不降级；只翻过标题（translator 为空）的文章
        # 即使全部命中翻译记忆，也要落 translator，标记成全文已翻，否则会被反复选中。
        return stats
    if stats.needs_review:
        parts = []
        if stats.rejected:
            parts.append("结构校验未通过的段落 " + ", ".join(map(str, stats.rejected)))
        if stats.oversized:
            parts.append("超过单次请求上限的段落 " + ", ".join(map(str, stats.oversized)))
        error = "；".join(parts)
        store.set_status(
            conn,
            url,
            Status.NEEDS_REVIEW,
            translator=backend,
            translated_at=now,
            title_zh=title_zh,
            error=error,
        )
    else:
        store.set_status(
            conn,
            url,
            Status.TRANSLATED,
            translator=backend,
            translated_at=now,
            title_zh=title_zh,
            error=None,
        )
    return stats


def _unique_segments(
    conn: sqlite3.Connection, urls: Iterable[str], glossary: Glossary | None = None
) -> list[Segment]:
    """这些文章的段落（跨文章去重）。给了 glossary 时去掉不送翻译的（固定译文、说话人标签）。"""
    seen: set[str] = set()
    out: list[Segment] = []
    for url in urls:
        segments = store.get_segments(conn, url)
        skip: dict[str, tuple[str, str]] = {}
        if glossary is not None:
            row = store.get_article(conn, url)
            interview = row is not None and is_interview(row["tags"])
            skip = untranslated_units(segments, glossary.fixed, interview=interview)
        for seg in segments:
            if seg.src_hash not in seen and seg.src_hash not in skip:
                seen.add(seg.src_hash)
                out.append(seg)
    return out


def _title_units(conn: sqlite3.Connection, urls: Iterable[str]) -> list[str]:
    """这些文章标题的翻译单元（去重，没有标题的跳过）。"""
    out: dict[str, None] = {}
    for url in urls:
        row = store.get_article(conn, url)
        title = None if row is None else row["title"]
        if title and str(title).strip():
            out[title_unit(str(title))] = None
    return list(out)


def estimate_chars(
    conn: sqlite3.Connection,
    urls: Iterable[str],
    backend: str,
    glossary: Glossary,
    *,
    retry_rejected: bool = True,
) -> int:
    """估算翻完这些文章还要多少字符（只算翻译记忆未命中的段落和标题，跨文章去重）。

    和 base.FREE_MONTHLY_CHARS 比较，就知道这个月的额度够不够。
    retry_rejected=False 时不算同一术语版本下已有 rejected 译文的段落（translate 不会重翻它们）。
    按送出去的 HTML 长度算，是上界。
    """
    urls = list(urls)
    units = [seg.html for seg in _unique_segments(conn, urls, glossary)] + _title_units(conn, urls)
    total = 0
    for unit in dict.fromkeys(units):
        if lookup_translation(conn, unit, backend, glossary) is not None:
            continue
        if not retry_rejected:
            entry = store.tm_lookup(conn, segment_hash(unit), backend, glossary.version_for(unit))
            if entry is not None:
                continue
        total += len(unit)
    return total


def missing_titles(
    conn: sqlite3.Connection,
    site: str | None = None,
    rules: SectionRules | None = None,
) -> list[str]:
    """有英文标题、还没有中文标题的文章。

    v2 分层翻译：入库时只翻标题，所以 extracted / filtered（还没翻全文的）也在这里；
    已 translated / published 的是 PR #7 之前翻的、当时没翻标题。
    给了 rules 就只返回采集栏目里的文章（titles 阶段不翻未选栏目的标题，避免浪费额度）。
    标题翻译已失败过的（qc_issues 里有 TITLE_FAILED_MARK）不返回：不再重送。
    """
    rows = store.list_rows(
        conn, [Status.EXTRACTED, Status.FILTERED, Status.TRANSLATED, Status.PUBLISHED], site
    )
    out: list[str] = []
    for r in rows:
        if not r["title"] or not str(r["title"]).strip() or r["title_zh"]:
            continue
        if TITLE_FAILED_MARK in _qc_items(r["qc_issues"]):
            continue
        url = str(r["url"])
        if rules is not None and not rules.collects(url, r["section_key"]):
            continue
        out.append(url)
    return out


TITLE_FAILED_MARK = "标题翻译失败（结构校验未通过）"


def _qc_items(value: str | None) -> list[str]:
    """qc_issues 是 JSON 字符串数组；解析成列表（异常/非列表时兜底成单元素）。"""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return [value]
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return [str(parsed)]


def _append_qc_issue(value: str | None, issue: str) -> str:
    items = _qc_items(value)
    if issue not in items:
        items.append(issue)
    return json.dumps(items, ensure_ascii=False)


def backfill_titles(
    conn: sqlite3.Connection, urls: Iterable[str], translator: Translator, glossary: Glossary
) -> int:
    """只补翻标题（走翻译记忆，命中就不花额度），返回补上了几篇。

    已发布的文章补上后退回 translated，下次 export 用中文标题重新生成；但只翻过标题、还没翻
    全文（translator 为空）的文章保持 published 不动，避免造出「translated 却 translator 为空」
    的坏状态（translated 只表示全文翻过）。
    标题翻译/结构校验失败（_translate_title 返回 None）只记一次失败：在 qc_issues 里打标记、
    不改文章状态（needs_review 会让文章从导出里消失），title_zh 留空、导出用英文标题回填；
    missing_titles 会跳过带这个标记的文章，不再重送。
    额度用完抛 QuotaStopped、后端不可用抛 BackendStopped，已补的都已落库。
    """
    done = 0
    for url in urls:
        row = store.get_article(conn, url)
        if row is None or row["title_zh"] or not row["title"]:
            continue
        stats = TranslateStats(url=url)
        segments = store.get_segments(conn, url)
        context = plain_text(segments[0].html)[:CONTEXT_CHARS] if segments else ""
        try:
            title_zh = _translate_title(conn, row["title"], translator, glossary, context, stats)
        except QuotaExceededError as e:
            raise QuotaStopped(stats, f"额度用完（{e}）") from e
        except TranslatorUnavailableError as e:
            raise BackendStopped(stats, str(e)) from e
        except ArticleTranslationError:
            # 这一篇标题的请求有问题：跳过这一篇（下一轮再试），别拖垮整轮补标题
            continue
        if title_zh is None:
            # 翻译/结构校验失败：只打标记、不改状态，title_zh 留空（导出用英文标题回填）
            store.set_fields(
                conn, url, qc_issues=_append_qc_issue(row["qc_issues"], TITLE_FAILED_MARK)
            )
            continue
        status = Status(row["status"])
        if status is Status.PUBLISHED and row["translator"]:
            status = Status.TRANSLATED
        store.set_status(conn, url, status, title_zh=title_zh)
        done += 1
    return done


def glossary_upgrade_cost(
    conn: sqlite3.Connection,
    urls: Iterable[str],
    backend: str,
    old: Glossary,
    new: Glossary,
) -> int:
    """术语表从 old 换成 new，会让多少字符的已翻段落和标题重翻（换之前先看一眼）。

    匹配规则版本（SEGMENT_RULES）也升过级时，翻译记忆里存的是旧规则前缀的版本（"g1-…"），
    所以按旧规则的前缀也查一遍：这部分同样要重翻。
    """
    urls = list(urls)
    units = [seg.html for seg in _unique_segments(conn, urls, new)] + _title_units(conn, urls)
    total = 0
    for unit in dict.fromkeys(units):
        after = new.version_for(unit)
        h = segment_hash(unit)
        if store.tm_get(conn, h, backend, after) is not None:
            continue
        before = old.version_for(unit)
        candidates = {before} | {
            before.replace(f"{SEGMENT_RULES}-", f"{r}-", 1) for r in PREVIOUS_SEGMENT_RULES
        }
        if any(store.tm_get(conn, h, backend, v) is not None for v in candidates):
            total += len(unit)
    return total
