import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, QuotaExceededError, Segment, Status
from qbkb.translate import (
    BackendStopped,
    FakeTranslator,
    Glossary,
    QuotaStopped,
    TranslationStopped,
    TranslatorUnavailableError,
    count_chars,
    estimate_chars,
    glossary_upgrade_cost,
    needs_review,
    translate_article,
)
from qbkb.translate.footnotes import sent_length

URL = "https://example.test/insights/a"

# 构造的段落（自己写的句子，不是任何真实文章）
PARAS = [
    "<h2>Why drawdown matters</h2>",
    '<p>A <a href="https://example.test/x">trend following</a> rule can lose money.</p>',
    "<p>Momentum is one factor among many.</p>",
    '<figure><img src="https://example.test/i.png"><figcaption>Chart</figcaption></figure>',
    "<p>Volatility targeting keeps risk stable.</p>",
]
TOTAL = count_chars(PARAS)
LINK = '<a href="https://example.test/x">'

AddArticle = Callable[..., str]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


@pytest.fixture
def glossary() -> Glossary:
    return Glossary("1", {"drawdown": "回撤", "trend following": "趋势跟踪", "momentum": "动量"})


@pytest.fixture
def add_article(conn: sqlite3.Connection) -> AddArticle:
    def add(paras: list[str] = PARAS, url: str = URL) -> str:
        store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug=url[-1:]))
        store.replace_segments(conn, url, [Segment(idx=i, html=h) for i, h in enumerate(paras)])
        return url

    return add


def _row(conn: sqlite3.Connection, url: str = URL) -> sqlite3.Row:
    row = store.get_article(conn, url)
    assert row is not None
    return row


def _tm(
    conn: sqlite3.Connection, pos: int, glossary: Glossary, backend: str = "fake"
) -> str | None:
    seg = store.get_segments(conn, URL)[pos]
    return store.tm_get(conn, seg.src_hash, backend, glossary.version_for(seg.html))


def _sent(t: FakeTranslator) -> list[str]:
    return [s for call in t.calls for s in call]


def test_first_run_translates_everything(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.segments, stats.tm_hits, stats.translated) == (5, 0, 5)
    assert stats.chars == TOTAL == t.chars_used
    assert not stats.needs_review
    assert len(t.calls) == 1  # 一批送完
    row = _row(conn)
    assert row["status"] == Status.TRANSLATED
    assert row["translator"] == "fake"
    assert row["translated_at"] and row["error"] is None
    assert _tm(conn, 0, glossary) == "<h2>【译】Why 回撤 matters</h2>"


def test_tm_hits_are_skipped(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    translate_article(conn, URL, FakeTranslator(), glossary)
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.tm_hits, stats.translated, stats.chars) == (5, 0, 0)
    assert t.calls == []
    # 另一篇文章复用了其中两段：只翻新的那段
    other = add_article([PARAS[0], "<p>New text.</p>", PARAS[2]], url="https://example.test/b")
    stats = translate_article(conn, other, t, glossary)
    assert (stats.tm_hits, stats.translated) == (2, 1)
    assert t.calls == [["<p>New text.</p>"]]


def test_misses_are_batched_together_with_neighbour_context(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    footer = "<p>Copyright notice.</p>"
    add_article([*PARAS[:2], footer, *PARAS[2:], footer])
    # 中间那段已有译文：剩下的未命中段落被它和重复段落隔开，也仍然合成一批
    seg = store.get_segments(conn, URL)[3]
    store.tm_put(
        conn, seg.src_hash, "fake", seg.html, "<p>已有</p>", glossary.version_for(seg.html)
    )
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert t.calls == [[PARAS[0], PARAS[1], footer, PARAS[3], PARAS[4]]]
    assert (stats.tm_hits, stats.translated, stats.duplicates) == (1, 5, 1)
    # context 来自原文里的邻居：已命中的那段（不在批里）是参考，批内段落不重复放进去
    (ctx,) = t.contexts
    assert "Momentum is one factor among many." in ctx
    assert "Why drawdown" not in ctx and "<" not in ctx


def test_duplicate_segments_translated_once(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    footer = "<p>Copyright notice.</p>"
    add_article(["<p>One.</p>", footer, "<p>Two.</p>", footer])
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.translated, stats.duplicates) == (3, 1)
    assert _sent(t).count(footer) == 1


def test_glossary_change_only_retranslates_affected_segments(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    urls = [add_article()]
    translate_article(conn, URL, FakeTranslator(), glossary)
    # 改了 momentum（只出现在第 2 段），新增一个文中没有的词条，version 也递增了
    new = Glossary("2", {**glossary, "momentum": "动能", "carry": "套息"})
    assert glossary_upgrade_cost(conn, urls, "fake", glossary, new) == len(PARAS[2])
    assert estimate_chars(conn, urls, "fake", new) == len(PARAS[2])
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, new)
    assert (stats.tm_hits, stats.translated) == (4, 1)
    assert t.calls == [[PARAS[2]]]
    assert "动能" in (_tm(conn, 2, new) or "")
    # 只新增文中没有的词条：一段都不用重翻
    newer = Glossary("3", {**new, "risk parity": "风险平价"})
    assert glossary_upgrade_cost(conn, urls, "fake", new, newer) == 0


def test_quota_stop_then_resume_without_double_billing(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    budget = count_chars(PARAS[:2])
    first = FakeTranslator(quota_chars=budget)
    # 批上限压到最长段落的长度，迫使每批大约一段
    with pytest.raises(QuotaStopped) as exc:
        translate_article(conn, URL, first, glossary, max_batch_chars=max(map(len, PARAS)))
    assert isinstance(exc.value, QuotaExceededError)
    assert isinstance(exc.value, TranslationStopped)
    assert exc.value.stats.translated == 2
    assert _row(conn)["status"] == Status.DISCOVERED  # 状态没推进

    second = FakeTranslator()  # 下个月，额度恢复
    stats = translate_article(conn, URL, second, glossary)
    assert (stats.tm_hits, stats.translated) == (2, 3)
    assert first.chars_used + second.chars_used == TOTAL  # 每段只计费一次
    assert _sent(second) == PARAS[2:]


class BreakLinks(FakeTranslator):
    """把链接标签吃掉。only_in_batches=True 时只在多段的批量请求里出错。"""

    def __init__(
        self, *, times: int = 99, only_in_batches: bool = False, quota_chars: int | None = None
    ) -> None:
        super().__init__(quota_chars=quota_chars)
        self.left = times
        self.only_in_batches = only_in_batches

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        out = super().translate(segments, glossary, context)
        if self.only_in_batches and len(segments) == 1:
            return out
        fixed = []
        for s in out:
            if LINK in s and self.left > 0:
                self.left -= 1
                s = s.replace(LINK, "").replace("</a>", "")
            fixed.append(s)
        return fixed


def test_retry_is_single_segment_without_context(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    # 最后一段已有译文，所以第一批有 context
    seg = store.get_segments(conn, URL)[4]
    store.tm_put(
        conn, seg.src_hash, "fake", seg.html, "<p>已有</p>", glossary.version_for(seg.html)
    )
    t = BreakLinks(only_in_batches=True)
    stats = translate_article(conn, URL, t, glossary)
    assert stats.retried == 1 and not stats.needs_review
    assert t.contexts[0].startswith("Volatility")
    assert t.calls[-1] == [PARAS[1]] and t.contexts[-1] == ""
    assert stats.chars == count_chars(PARAS[:4]) + len(PARAS[1])
    assert LINK in (_tm(conn, 1, glossary) or "")
    assert _row(conn)["status"] == Status.TRANSLATED


def test_rejected_translation_is_quarantined_not_reused(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    stats = translate_article(conn, URL, BreakLinks(), glossary)
    assert stats.retried == 1 and stats.rejected == [1]
    # 坏译文不在正常的翻译记忆里，只在隔离区
    assert _tm(conn, 1, glossary) is None
    assert _tm(conn, 1, glossary) is None  # tm_get 不返回 rejected
    entry = store.tm_lookup(
        conn, Segment(1, PARAS[1]).src_hash, "fake", glossary.version_for(PARAS[1])
    )
    assert entry is not None and entry.quality == store.TM_REJECTED
    row = _row(conn)
    assert row["status"] == Status.NEEDS_REVIEW
    assert "1" in row["error"]
    assert needs_review(row)

    # 重跑：不再花额度去重翻，仍然报告需要复查
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert t.calls == [] and stats.rejected == [1] and stats.tm_hits == 4
    assert needs_review(_row(conn))

    # 强制重翻开关：这次后端正常了，文章推进到 translated
    stats = translate_article(conn, URL, t, glossary, retry_rejected=True)
    assert t.calls == [[PARAS[1]]] and stats.rejected == []
    row = _row(conn)
    assert row["status"] == Status.TRANSLATED and row["error"] is None
    assert not needs_review(row)


def test_bad_hit_in_tm_is_not_silently_reused(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    translate_article(conn, URL, FakeTranslator(), glossary)
    # 模拟旧版本代码写进正常翻译记忆的坏译文
    seg = store.get_segments(conn, URL)[1]
    store.tm_put(conn, seg.src_hash, "fake", seg.html, "<p>坏</p>", glossary.version_for(seg.html))
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert stats.rejected == [1] and t.calls == []
    assert needs_review(_row(conn))
    translate_article(conn, URL, t, glossary, retry_rejected=True)
    assert t.calls == [[PARAS[1]]]
    assert _row(conn)["status"] == Status.TRANSLATED


def test_quota_during_retry_keeps_good_segments(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    t = BreakLinks(quota_chars=TOTAL)  # 第一批刚好用完额度，重翻时超额
    with pytest.raises(QuotaStopped) as exc:
        translate_article(conn, URL, t, glossary)
    assert exc.value.stats.translated == 4
    assert [_tm(conn, i, glossary) is not None for i in range(5)] == [True, False, True, True, True]


def test_oversized_segment_is_recorded_not_blocking(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    huge = "<p>" + "x" * 200 + "</p>"
    add_article([PARAS[0], huge, PARAS[2]])
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary, max_batch_chars=100)
    assert stats.oversized == [1] and stats.translated == 2
    assert huge not in _sent(t)
    row = _row(conn)
    assert needs_review(row) and "超过单次请求上限" in row["error"]


def test_backend_unavailable_stops_with_stats(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    class Down(FakeTranslator):
        def translate(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
        ) -> list[str]:
            raise TranslatorUnavailableError("429")

    add_article()
    with pytest.raises(BackendStopped) as exc:
        translate_article(conn, URL, Down(), glossary)
    assert not isinstance(exc.value, QuotaExceededError)
    assert exc.value.stats.translated == 0
    assert _row(conn)["status"] == Status.DISCOVERED


class NoContext:
    """只实现 models.Translator 的最小后端：接受 context 但不使用。"""

    def __init__(self) -> None:
        self.inner = FakeTranslator(name="plain")

    @property
    def name(self) -> str:
        return "plain"

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        return self.inner.translate(segments, glossary)


def test_backend_ignoring_context(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    translate_article(conn, URL, NoContext(), glossary)
    row = _row(conn)
    assert row["status"] == Status.TRANSLATED and row["translator"] == "plain"


def test_wrong_output_length_is_an_error(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    class Short(FakeTranslator):
        def translate(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
        ) -> list[str]:
            return super().translate(segments, glossary, context)[:-1]

    add_article()
    with pytest.raises(RuntimeError):
        translate_article(conn, URL, Short(), glossary)


def test_article_without_segments(conn: sqlite3.Connection, glossary: Glossary) -> None:
    with pytest.raises(ValueError):
        translate_article(conn, URL, FakeTranslator(), glossary)


def test_estimate_chars(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    a = add_article()
    b = add_article([PARAS[0], "<p>New.</p>"], url="https://example.test/b")
    # 跨文章去重：PARAS[0] 只算一次
    assert estimate_chars(conn, [a, b], "fake", glossary) == TOTAL + len("<p>New.</p>")
    translate_article(conn, a, FakeTranslator(), glossary)
    assert estimate_chars(conn, [a, b], "fake", glossary) == len("<p>New.</p>")


def _sup_segments() -> list[str]:
    # 脚注占位展开后（<sup>N</sup> → <a href="#qbkb-sup-N">†</a>）比原文长
    return [
        "<p>" + "x" * 60 + "<sup>3</sup></p>",
        "<p>" + "y" * 60 + "<sup>4</sup></p>",
    ]


class _IdentityTranslator:
    """原样返回、只记录每次调用的段落：绕过 FakeTranslator 对 <sup> 内容的改写，专心测分批。"""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        self.calls.append(list(segments))
        return list(segments)


def test_batches_by_sent_length_not_raw_length(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    segs = _sup_segments()
    add_article(segs)
    # 原文两段合起来 ≤ limit，但脚注展开后 > limit：必须分成两批，否则后端会再切成两个请求
    limit = sum(map(len, segs)) + 5
    assert sum(map(sent_length, segs)) > limit
    t = _IdentityTranslator()
    stats = translate_article(conn, URL, t, glossary, max_batch_chars=limit)
    assert stats.translated == 2
    assert t.calls == [[segs[0]], [segs[1]]]  # 每批一段，一段对应一个请求


def test_batches_by_unit_count_not_paragraph_count(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    from qbkb.translate.lines import split_lines

    paras = [f"<p>para {i} text.</p>" for i in range(40)]
    lines = "<br>".join(f"{i}. line {i}" for i in range(1, 16))
    footnote = f"<p>{lines}</p>"
    add_article(paras + [footnote])

    class UnitAware(_IdentityTranslator):
        def request_units(self, html: str) -> tuple[int, int]:
            plan = split_lines(html)
            pieces = plan.lines if plan is not None else [html]
            return len(pieces), sum(len(p) for p in pieces)

    t = UnitAware()
    stats = translate_article(conn, URL, t, glossary)
    # 40 段（各 1 单元）+ 1 个 15 行脚注列表（15 单元）= 55 单元 > 50：必须拆成两批，
    # 每个请求的单元数都不超过 50，不能按段落数（41 段 ≤ 50）合成一批让后端再切
    assert stats.translated == 41
    assert [len(c) for c in t.calls] == [40, 1]


def test_later_batch_failure_preserves_earlier_batch(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    segs = _sup_segments()
    add_article(segs)
    limit = sum(map(len, segs)) + 5

    class QuotaOnSecond(_IdentityTranslator):
        def __init__(self) -> None:
            super().__init__()
            self.calls_made = 0

        def translate(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
        ) -> list[str]:
            self.calls_made += 1
            if self.calls_made > 1:
                raise QuotaExceededError("额度在这批中间用完")
            return super().translate(segments, glossary, context)

    t = QuotaOnSecond()
    with pytest.raises(QuotaStopped) as exc:
        translate_article(conn, URL, t, glossary, max_batch_chars=limit)
    # 第一批已经翻好、写进翻译记忆；第二批额度用完，前面不丢
    assert exc.value.stats.translated == 1
    assert _tm(conn, 0, glossary) is not None
    assert _tm(conn, 1, glossary) is None
