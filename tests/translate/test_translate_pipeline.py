import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, QuotaExceededError, Segment, Status
from qbkb.translate import (
    FakeTranslator,
    Glossary,
    TranslationStopped,
    count_chars,
    estimate_chars,
    translate_article,
)

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


def _tm_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM translation_memory").fetchone()[0])


def test_first_run_translates_everything(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.segments, stats.tm_hits, stats.translated) == (5, 0, 5)
    assert stats.chars == TOTAL == t.chars_used
    assert stats.complete and stats.structure_failures == []
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.TRANSLATED
    assert row["translator"] == "fake"
    assert row["translated_at"] and row["error"] is None
    seg = store.get_segments(conn, URL)[0]
    assert (
        store.tm_get(conn, seg.src_hash, "fake", glossary.tm_version)
        == "<h2>【译】Why 回撤 matters</h2>"
    )


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


def test_duplicate_segments_translated_once(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    footer = "<p>Copyright notice.</p>"
    add_article(["<p>One.</p>", footer, "<p>Two.</p>", footer])
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.translated, stats.duplicates) == (3, 1)
    assert sum(c.count(footer) for c in t.calls) == 1
    assert stats.complete


def test_glossary_change_retranslates(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    translate_article(conn, URL, FakeTranslator(), glossary)
    new = Glossary("2", {**glossary, "momentum": "动能"})
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, new)
    assert (stats.tm_hits, stats.translated) == (0, 5)
    seg = store.get_segments(conn, URL)[2]
    assert "动能" in (store.tm_get(conn, seg.src_hash, "fake", new.tm_version) or "")
    # 主键是 (src_hash, backend)：新版本覆盖旧版本，旧版本不再命中
    assert store.tm_get(conn, seg.src_hash, "fake", glossary.tm_version) is None
    assert _tm_rows(conn) == 5


def test_quota_stop_then_resume_without_double_billing(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    # 每批最多约 1 段；额度只够前两段
    budget = count_chars(PARAS[:2])
    first = FakeTranslator(quota_chars=budget)
    with pytest.raises(TranslationStopped) as exc:
        translate_article(conn, URL, first, glossary, max_batch_chars=1)
    assert isinstance(exc.value, QuotaExceededError)
    assert exc.value.stats.translated == 2
    assert not exc.value.stats.complete
    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.DISCOVERED  # 状态没推进
    assert _tm_rows(conn) == 2

    second = FakeTranslator()  # 下个月，额度恢复
    stats = translate_article(conn, URL, second, glossary)
    assert (stats.tm_hits, stats.translated) == (2, 3)
    assert stats.complete
    # 每段只计费一次
    assert first.chars_used + second.chars_used == TOTAL
    assert [s for call in second.calls for s in call] == PARAS[2:]


class FlakyStructure(FakeTranslator):
    """对含 <a> 的段落把链接标签吃掉，前 broken_times 次都这样。"""

    def __init__(self, broken_times: int) -> None:
        super().__init__()
        self.broken_left = broken_times

    def translate_with_context(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str
    ) -> list[str]:
        out = super().translate_with_context(segments, glossary, context)
        fixed: list[str] = []
        for s in out:
            if "<a " in s and self.broken_left > 0:
                self.broken_left -= 1
                s = s.replace('<a href="https://example.test/x">', "").replace("</a>", "")
            fixed.append(s)
        return fixed


def test_structure_mismatch_is_retried(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    t = FlakyStructure(broken_times=1)
    stats = translate_article(conn, URL, t, glossary)
    assert stats.retried == 1
    assert stats.structure_failures == []
    assert stats.chars == TOTAL + len(PARAS[1])  # 重翻的那段多花一次
    assert t.calls[-1] == [PARAS[1]]
    seg = store.get_segments(conn, URL)[1]
    assert "<a href" in (store.tm_get(conn, seg.src_hash, "fake", glossary.tm_version) or "")


def test_structure_mismatch_twice_is_recorded(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    stats = translate_article(conn, URL, FlakyStructure(broken_times=2), glossary)
    assert stats.retried == 1
    assert stats.structure_failures == [1]
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.TRANSLATED
    assert "1" in row["error"]
    # 重跑：命中翻译记忆，不再花额度，但问题仍然报告出来
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, glossary)
    assert t.calls == [] and stats.structure_failures == [1]


def test_context_comes_from_neighbours(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    # 先翻好中间那段，剩下的待翻段落在它两侧，被它隔开成两批
    seg2 = store.get_segments(conn, URL)[2]
    store.tm_put(conn, seg2.src_hash, "fake", seg2.html, "<p>已有</p>", glossary.tm_version)
    t = FakeTranslator()
    translate_article(conn, URL, t, glossary)
    assert t.calls == [PARAS[:2], PARAS[3:]]
    # 第一批的 context 是后面的原文，第二批的是前面的原文；context 不含 HTML 标签
    assert t.contexts[0].startswith("Momentum is one factor")
    assert "Momentum is one factor among many." in t.contexts[1]
    assert "<" not in t.contexts[0] + t.contexts[1]


class NoContext:
    """只实现 models.Translator 的最小后端。"""

    def __init__(self) -> None:
        self.inner = FakeTranslator(name="plain")

    @property
    def name(self) -> str:
        return "plain"

    def translate(self, segments: Sequence[str], glossary: Mapping[str, str]) -> list[str]:
        return self.inner.translate(segments, glossary)


def test_backend_without_context_support(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    add_article()
    stats = translate_article(conn, URL, NoContext(), glossary)
    assert stats.complete
    row = store.get_article(conn, URL)
    assert row is not None and row["translator"] == "plain"


def test_wrong_output_length_is_an_error(
    conn: sqlite3.Connection, add_article: AddArticle, glossary: Glossary
) -> None:
    class Short(FakeTranslator):
        def translate_with_context(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str
        ) -> list[str]:
            return super().translate_with_context(segments, glossary, context)[:-1]

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
