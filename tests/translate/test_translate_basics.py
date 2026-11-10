from pathlib import Path

import pytest

from qbkb.models import QuotaExceededError
from qbkb.translate import FakeTranslator, Glossary, load_glossary, same_structure
from qbkb.translate.base import chunk_texts, count_chars
from qbkb.translate.structure import plain_text, structure_signature


def test_repo_glossary_loads() -> None:
    g = load_glossary()
    assert g.version == "1"
    assert g["drawdown"] == "回撤"
    assert g.tm_version.startswith("1-")


def test_tm_version_tracks_content_not_order() -> None:
    a = Glossary("1", {"a": "甲", "b": "乙"})
    b = Glossary("1", {"b": "乙", "a": "甲"})
    c = Glossary("1", {"a": "甲", "b": "丙"})
    assert a.tm_version == b.tm_version
    # 忘了递增 version 也能区分
    assert a.tm_version != c.tm_version
    assert Glossary("2", dict(a)).tm_version != a.tm_version


@pytest.mark.parametrize(
    "body",
    [
        "terms: {a: b}\n",  # 没有 version
        "version: 1\nterms: [a]\n",
        "version: 1\nterms: {a: 1}\n",
        "version: 1\nterms: {' a': b}\n",
    ],
)
def test_bad_glossary_rejected(tmp_path: Path, body: str) -> None:
    p = tmp_path / "glossary.yaml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        load_glossary(p)


def test_structure_counts_tags_links_images() -> None:
    src = '<p>Hi <a href="u">x</a> <img src="i.png"></p>'
    assert same_structure(src, '<p>你好 <a href="u">某</a> <img src="i.png"></p>')
    assert not same_structure(src, '<p>你好 <a href="u">某</a></p>')  # 丢了图片
    assert not same_structure(src, '<p>你好 <a href="v">某</a> <img src="i.png"></p>')  # 链接变了
    assert not same_structure(src, '<p>你好 某 <img src="i.png"></p>')  # 丢了标签
    assert structure_signature(src)["<a>"] == 1


def test_plain_text() -> None:
    assert plain_text("<p>a  <b>b</b>\n c &amp; d</p>") == "a b c & d"


def test_chunk_texts_limits() -> None:
    texts = ["aaaa", "bb", "cccccc", "d", "e"]
    assert chunk_texts(texts, max_items=2, max_chars=100) == [range(0, 2), range(2, 4), range(4, 5)]
    assert chunk_texts(texts, max_items=10, max_chars=6) == [range(0, 2), range(2, 3), range(3, 5)]
    # 单条超长也单独成批，不丢
    assert chunk_texts(["x" * 10], max_items=5, max_chars=3) == [range(0, 1)]
    assert chunk_texts([], 5, 5) == []
    assert count_chars(texts) == 14


def test_fake_translator_keeps_tags_and_applies_glossary() -> None:
    t = FakeTranslator()
    src = '<p>Trend following and <a href="u">drawdown</a></p>'
    (out,) = t.translate([src], {"trend following": "趋势跟踪", "drawdown": "回撤"})
    assert out == '<p>【译】趋势跟踪 and <a href="u">【译】回撤</a></p>'
    assert same_structure(src, out)
    assert t.translate([src], {}) == t.translate([src], {})  # 确定性
    assert t.chars_used == 3 * len(src)


def test_fake_translator_quota_rejects_whole_batch() -> None:
    t = FakeTranslator(quota_chars=10)
    t.translate(["12345"], {})
    with pytest.raises(QuotaExceededError):
        t.translate(["123", "456"], {})
    assert t.chars_used == 5
    assert len(t.calls) == 1
