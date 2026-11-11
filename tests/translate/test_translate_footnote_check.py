"""脚注不能悄悄丢（PR #8 审查）：结构校验显式比较脚注编号，丢了就重翻、再丢就进 needs_review。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import deepl
import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, Segment, Status, segment_hash
from qbkb.translate import DeepLTranslator, Glossary, same_structure, translate_article
from qbkb.translate.footnotes import protect_sups, restore_sups
from qbkb.translate.structure import footnote_marks

URL = "https://example.test/insights/f"
SRC = "<p>Growth of 3 percent at a zero discount rate<sup>3</sup> is rare.</p>"
PLACEHOLDER = '<a href="#qbkb-sup-3">†</a>'


def test_footnote_marks() -> None:
    assert footnote_marks("<p>a<sup>2</sup> b<sup> 10 </sup> c<sup>th</sup></p>") == ["10", "2"]
    assert footnote_marks("<p>3 and 10, no sups</p>") == []


def test_same_structure_checks_footnote_numbers() -> None:
    assert same_structure(SRC, "<p>零折现率<sup>3</sup>下 3% 的增长很少见。</p>")
    # 上标整个没了 / 标签没了数字留在正文 / 数字被改 / 数字跑到上标外面
    assert not same_structure(SRC, "<p>零折现率下 3% 的增长很少见。</p>")
    assert not same_structure(SRC, "<p>零折现率3下 3% 的增长很少见。</p>")
    assert not same_structure(SRC, "<p>零折现率<sup>4</sup>下 3% 的增长很少见。</p>")
    assert not same_structure(SRC, "<p>零折现率3<sup></sup>下 3% 的增长很少见。</p>")
    # 两个脚注互换位置可以（语序不同），少一个不行
    two = "<p>a<sup>1</sup> b<sup>2</sup></p>"
    assert same_structure(two, "<p>乙<sup>2</sup> 甲<sup>1</sup></p>")
    assert not same_structure(two, "<p>甲<sup>1</sup> 乙<sup>1</sup></p>")
    # 不是纯数字的上标不比内容
    assert same_structure("<p>the 20<sup>th</sup></p>", "<p>第 20<sup>个</sup></p>")


def test_dropped_placeholder_fails_the_check() -> None:
    sent = protect_sups(SRC)
    # 占位链接整个丢失，数字留在正文里：正文里本来就有一个 3，数数字是看不出来的
    got = restore_sups(sent.replace(PLACEHOLDER, "3"))
    assert "<sup>" not in got and not same_structure(SRC, got)
    # 占位链接连数字一起丢失
    got = restore_sups(sent.replace(PLACEHOLDER, ""))
    assert not same_structure(SRC, got)


def test_placeholder_with_only_neighbouring_text_is_restored() -> None:
    # 链接还在，里面只剩旁边的字、数字没了：以 href 为准还原
    got = restore_sups('<p>零折现<a href="#qbkb-sup-3">率</a>下 3% 的增长很少见。</p>')
    assert got == "<p>零折现率<sup>3</sup>下 3% 的增长很少见。</p>"
    assert same_structure(SRC, got)


def test_body_number_equal_to_the_footnote_number() -> None:
    # 正文数字被一起包进了占位链接："3<sup>3</sup>" 的往返
    src = "<p>It grew by 3<sup>3</sup> last year.</p>"
    assert protect_sups(src) == f"<p>It grew by 3{PLACEHOLDER} last year.</p>"
    got = restore_sups('<p>去年增长了<a href="#qbkb-sup-3">33</a>。</p>')
    assert got == "<p>去年增长了3<sup>3</sup>。</p>" and same_structure(src, got)
    # 正文里别处有同一个数字，不受影响
    got = restore_sups(f"<p>3 年里，折现率{PLACEHOLDER}是 3%。</p>")
    assert got == "<p>3 年里，折现率<sup>3</sup>是 3%。</p>"


class DroppingClient:
    """假的 deepl 客户端：前 drops 次请求把占位链接整个丢掉（只留数字），之后原样返回。"""

    def __init__(self, drops: int) -> None:
        self.drops = drops
        self.calls: list[list[str]] = []

    def translate_text(self, text: Any, **kwargs: Any) -> Any:
        texts = list(text)
        self.calls.append(texts)
        if self.drops > 0:
            self.drops -= 1
            texts = [t.replace(PLACEHOLDER, "3") for t in texts]
        return [deepl.TextResult(t, "EN", None) for t in texts]

    def list_glossaries(self) -> list[deepl.GlossaryInfo]:
        return []

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo:
        raise AssertionError("空术语表不应该创建")

    def get_usage(self) -> deepl.Usage:
        return deepl.Usage({"character_count": 0, "character_limit": 1_000_000})


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(url=URL, site="example", slug="f"))
    store.replace_segments(c, URL, [Segment(0, "<p>Plain paragraph.</p>"), Segment(1, SRC)])
    yield c
    c.close()


def _status(conn: sqlite3.Connection) -> str:
    row = store.get_article(conn, URL)
    assert row is not None
    return str(row["status"])


def test_lost_footnote_is_retranslated_once(conn: sqlite3.Connection) -> None:
    client = DroppingClient(drops=1)
    t = DeepLTranslator(client)
    glossary = Glossary("1", {})
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.retried, stats.rejected) == (1, [])
    assert _status(conn) == Status.TRANSLATED
    assert len(client.calls) == 2  # 整批一次 + 丢脚注的那段单独重翻一次
    entry = store.tm_lookup(conn, segment_hash(SRC), t.name, glossary.version_for(SRC))
    assert entry is not None and entry.quality == store.TM_OK and entry.tgt_html == SRC


def test_footnote_lost_twice_goes_to_needs_review(conn: sqlite3.Connection) -> None:
    client = DroppingClient(drops=2)
    t = DeepLTranslator(client)
    glossary = Glossary("1", {})
    stats = translate_article(conn, URL, t, glossary)
    assert (stats.retried, stats.rejected) == (1, [1])
    assert _status(conn) == Status.NEEDS_REVIEW
    entry = store.tm_lookup(conn, segment_hash(SRC), t.name, glossary.version_for(SRC))
    assert entry is not None and entry.quality == store.TM_REJECTED
    assert "<sup>" not in entry.tgt_html  # 留下的是丢了脚注的译文，等人工复查
