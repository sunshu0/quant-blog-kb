"""标题作为额外的翻译单元（Phase 2）：写进 title_zh，走翻译记忆。"""

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import (
    FakeTranslator,
    Glossary,
    QuotaStopped,
    lookup_translation,
    title_unit,
    translate_article,
    translated_title,
)

URL = "https://example.test/insights/t"
GLOSSARY = Glossary("1", {"momentum": "动量"})


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(URL, "example", "t"))
    store.replace_segments(c, URL, [Segment(0, "<p>Body text.</p>")])
    store.set_status(c, URL, Status.FILTERED, title="Momentum  & you")
    yield c
    c.close()


def test_title_unit_escapes_and_collapses_whitespace() -> None:
    assert title_unit("Momentum  &\n<you>") == "<p>Momentum &amp; &lt;you&gt;</p>"


def test_title_translated_once_and_reused(conn: sqlite3.Connection) -> None:
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, GLOSSARY)
    row = store.get_article(conn, URL)
    assert row is not None and row["title_zh"] == "【译】动量 & you"
    assert t.calls[-1] == [title_unit("Momentum & you")]
    assert stats.chars == len("<p>Body text.</p>") + len(title_unit("Momentum & you"))
    assert lookup_translation(conn, title_unit("Momentum & you"), "fake", GLOSSARY) is not None
    assert translated_title(conn, "Momentum & you", "fake", GLOSSARY) == "【译】动量 & you"

    again = FakeTranslator()
    store.set_status(conn, URL, Status.FILTERED)
    translate_article(conn, URL, again, GLOSSARY)
    assert again.calls == []  # 正文和标题都命中翻译记忆


def test_quota_during_title_keeps_body_progress(conn: sqlite3.Connection) -> None:
    body = len("<p>Body text.</p>")
    t = FakeTranslator(quota_chars=body)  # 正文刚好用完额度
    with pytest.raises(QuotaStopped):
        translate_article(conn, URL, t, GLOSSARY)
    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.FILTERED
    assert lookup_translation(conn, "<p>Body text.</p>", "fake", GLOSSARY) is not None


def test_no_title_means_no_extra_call(conn: sqlite3.Connection) -> None:
    store.set_status(conn, URL, Status.FILTERED, title=None)
    t = FakeTranslator()
    translate_article(conn, URL, t, GLOSSARY)
    assert len(t.calls) == 1
    row = store.get_article(conn, URL)
    assert row is not None and row["title_zh"] is None


class BrokenTitle(FakeTranslator):
    """标题译文丢了标签（结构校验不过）。"""

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        out = super().translate(segments, glossary, context)
        return [
            o.replace("<p>", "").replace("</p>", "") if "Momentum" in s else o
            for s, o in zip(segments, out, strict=True)
        ]


def test_bad_title_translation_does_not_block_article(conn: sqlite3.Connection) -> None:
    translate_article(conn, URL, BrokenTitle(), GLOSSARY)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.TRANSLATED and row["title_zh"] is None
