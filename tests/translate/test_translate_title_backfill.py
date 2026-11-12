"""PR #7 审查修复：估算额度算上标题；旧文章只补翻标题。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import (
    ArticleTranslationError,
    FakeTranslator,
    Glossary,
    QuotaStopped,
    backfill_titles,
    estimate_chars,
    missing_titles,
    title_unit,
    translate_article,
)

GLOSSARY = Glossary("1", {"momentum": "动量"})
BODY = "<p>Body text.</p>"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def add(conn: sqlite3.Connection, slug: str, title: str | None, status: Status) -> str:
    url = f"https://example.test/insights/{slug}"
    store.upsert_discovered(conn, DiscoveredUrl(url, "example", slug))
    store.replace_segments(conn, url, [Segment(0, BODY)])
    if status is Status.FILTERED:
        store.set_status(conn, url, Status.FILTERED, title=title)
        return url
    # 模拟 PR #7 之前翻的文章：翻译时还不翻标题，所以没有 title_zh、翻译记忆里也没有标题
    store.set_status(conn, url, Status.FILTERED, title=None)
    translate_article(conn, url, FakeTranslator(), GLOSSARY)
    store.set_status(conn, url, status, title=title, title_zh=None)
    return url


def test_estimate_includes_titles(conn: sqlite3.Connection) -> None:
    a = add(conn, "a", "Momentum basics", Status.FILTERED)
    b = add(conn, "b", "Momentum basics", Status.FILTERED)  # 同样的标题只算一次
    need = estimate_chars(conn, [a, b], "fake", GLOSSARY)
    assert need == len(BODY) + len(title_unit("Momentum basics"))
    translate_article(conn, a, FakeTranslator(), GLOSSARY)
    assert estimate_chars(conn, [a, b], "fake", GLOSSARY) == 0


def test_backfill_only_titles(conn: sqlite3.Connection) -> None:
    pub = add(conn, "p", "Momentum A", Status.PUBLISHED)
    tr = add(conn, "t", "Momentum B", Status.TRANSLATED)
    add(conn, "n", None, Status.TRANSLATED)  # 没有英文标题的不算
    add(conn, "f", "Momentum C", Status.FILTERED)  # 还没翻译的不归这里管
    assert missing_titles(conn, "example") == [pub, tr]

    t = FakeTranslator()
    assert backfill_titles(conn, missing_titles(conn), t, GLOSSARY) == 2
    # 只翻标题，正文一段都不送
    assert t.calls == [[title_unit("Momentum A")], [title_unit("Momentum B")]]
    p_row, t_row = store.get_article(conn, pub), store.get_article(conn, tr)
    assert p_row is not None and t_row is not None
    assert p_row["title_zh"] == "【译】动量 A"
    assert p_row["status"] == Status.TRANSLATED  # 已发布的退回，下次 export 重新生成
    assert t_row["status"] == Status.TRANSLATED
    assert missing_titles(conn) == []


def test_backfill_skips_article_translation_error(conn: sqlite3.Connection) -> None:
    from collections.abc import Mapping, Sequence

    a = add(conn, "a", "Momentum A", Status.TRANSLATED)
    b = add(conn, "b", "Momentum B", Status.TRANSLATED)

    class RejectA(FakeTranslator):
        def translate(
            self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
        ) -> list[str]:
            if any("Momentum A" in s for s in segments):
                raise ArticleTranslationError("DeepL 拒绝这次请求（HTTP 400）")
            return super().translate(segments, glossary, context)

    done = backfill_titles(conn, [a, b], RejectA(), GLOSSARY)
    assert done == 1  # a 这一篇跳过，不中断后面的
    assert store.get_article(conn, b)["title_zh"] == "【译】动量 B"  # type: ignore[index]
    assert store.get_article(conn, a)["title_zh"] is None  # type: ignore[index]
    assert missing_titles(conn) == [a]


def test_backfill_reuses_memory_and_stops_on_quota(conn: sqlite3.Connection) -> None:
    a = add(conn, "a", "Momentum A", Status.TRANSLATED)
    b = add(conn, "b", "Momentum B", Status.TRANSLATED)
    with pytest.raises(QuotaStopped):
        backfill_titles(
            conn, [a, b], FakeTranslator(quota_chars=len(title_unit("Momentum A"))), GLOSSARY
        )
    assert missing_titles(conn) == [b]  # 第一篇已经落库
    again = FakeTranslator()
    store.set_status(conn, a, Status.TRANSLATED, title_zh=None)
    assert backfill_titles(conn, [a, b], again, GLOSSARY) == 2
    assert again.calls == [[title_unit("Momentum B")]]  # a 的标题命中翻译记忆
