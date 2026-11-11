from pathlib import Path

import pytest

from qbkb.models import QuotaExceededError
from qbkb.translate import (
    FakeTranslator,
    Glossary,
    SegmentTooLargeError,
    load_glossary,
    same_structure,
)
from qbkb.translate.base import chunk_texts, count_chars
from qbkb.translate.glossary import NO_TERMS, case_variants
from qbkb.translate.structure import plain_text


def test_repo_glossary_loads() -> None:
    g = load_glossary()
    assert g.version == "2"
    assert g["drawdown"] == "回撤"
    # 2026-11-12 验收：有歧义的单词只收短语
    assert "carry" not in g and "factor" not in g
    assert g["carry trade"] == "套息交易" and g["value factor"] == "价值因子"


def test_segment_version_depends_only_on_terms_used() -> None:
    g = Glossary("1", {"drawdown": "回撤", "momentum": "动量"})
    plain = "<p>Nothing special here.</p>"
    dd = "<p>A deep <b>Drawdown</b>.</p>"  # 大小写不敏感
    assert g.version_for(plain) == NO_TERMS
    assert g.terms_in(dd) == {"drawdown": "回撤"}
    # 改了不相关的词条、递增了 version：这段的版本不变
    g2 = Glossary("2", {"drawdown": "回撤", "momentum": "动能", "carry": "套息"})
    assert g2.version_for(dd) == g.version_for(dd)
    assert g2.version_for(plain) == g.version_for(plain)
    # 改了这段用到的词条：版本变了
    g3 = Glossary("3", {"drawdown": "最大回撤", "momentum": "动量"})
    assert g3.version_for(dd) != g.version_for(dd)
    # 只看正文，不看属性；左边界匹配，复数也算
    assert g.version_for('<a href="/momentum">x</a>') == NO_TERMS
    assert g.terms_in("<p>momentums</p>") == {"momentum": "动量"}
    assert g.terms_in("<p>antimomentum</p>") == {}


def test_case_variants() -> None:
    v = case_variants({"trend following": "趋势跟踪", "alpha": "alpha", "CTA": "CTA"})
    assert v["Trend following"] == "趋势跟踪"
    assert v["Trend Following"] == "趋势跟踪"
    assert v["Alpha"] == "Alpha"  # 原文译文相同的词条，译文跟着原文变
    assert v["CTA"] == "CTA" and len([k for k in v if k.lower() == "cta"]) == 1
    # yaml 里显式写的变体优先
    assert case_variants({"beta": "贝塔", "Beta": "Beta系数"})["Beta"] == "Beta系数"


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


SRC = '<p>Hi <a href="u">x</a> <img src="i.png"></p>'


@pytest.mark.parametrize(
    "tgt",
    [
        '<p>你好 <a href="u">某</a> <img src="i.png"></p>',
        '<p><img src="i.png">你好 <a href="u">某</a></p>',  # 兄弟元素换了顺序：允许
        '<p>你好 <a href="u">某</a> <img src="i.png"/></p>',  # 自闭合写法
        # D7：链接只比地址集合，图片跑进链接、多一个 </a> 都不算错
        '<p>你好 <a href="u">某 <img src="i.png"></a></p>',
        '<p>你好 <a href="u">某</a></a> <img src="i.png"></p>',
    ],
)
def test_structure_same(tgt: str) -> None:
    assert same_structure(SRC, tgt)


@pytest.mark.parametrize(
    "tgt",
    [
        '<p>你好 <a href="u">某</a></p>',  # 丢了图片
        '<p>你好 <a href="v">某</a> <img src="i.png"></p>',  # 链接变了
        '<p>你好 某 <img src="i.png"></p>',  # 丢了标签
        '<p>你好 <a href="u">某 <img src="i.png"></p>',  # 链接没闭合
        '<p>你好 <a href="u">某</a> <img src="i.png"></cite></p>',  # 多了严格标签的结束标签
    ],
)
def test_structure_different(tgt: str) -> None:
    assert not same_structure(SRC, tgt)


def test_structure_nesting_order() -> None:
    # 严格标签的嵌套关系照比；放宽的 b / i 互相嵌套的先后不比
    assert not same_structure(
        "<p><cite><code>x</code></cite></p>", "<p><code><cite>x</cite></code></p>"
    )
    assert same_structure("<p><b><i>x</i></b></p>", "<p><i><b>x</b></i></p>")
    assert not same_structure("<p><b>x</b></p>", "<p><b>x</p></b>")  # 交叉嵌套：b 没正确闭合


def test_br_forms_are_equal() -> None:
    assert same_structure("<p>a<br>b</p>", "<p>a<br/>b</p>")
    assert same_structure("<p>a<br/>b</p>", "<p>a<br />b</p>")
    assert not same_structure("<p>a<br>b</p>", "<p>ab</p>")
    assert not same_structure("<p>a<br>b</p>", "<p>a<br><br>b</p>")


def test_plain_text() -> None:
    assert plain_text("<p>a  <b>b</b>\n c &amp; d</p>") == "a b c & d"


def test_chunk_texts_limits() -> None:
    texts = ["aaaa", "bb", "cccccc", "d", "e"]
    assert chunk_texts(texts, max_items=2, max_chars=100) == [range(0, 2), range(2, 4), range(4, 5)]
    assert chunk_texts(texts, max_items=10, max_chars=6) == [range(0, 2), range(2, 3), range(3, 5)]
    assert chunk_texts([], 5, 5) == []
    assert count_chars(texts) == 14


def test_chunk_texts_rejects_oversized() -> None:
    with pytest.raises(SegmentTooLargeError):
        chunk_texts(["ok", "x" * 10], max_items=5, max_chars=3)


def test_fake_translator_keeps_tags_and_applies_glossary() -> None:
    t = FakeTranslator()
    src = '<p>Trend following and <a href="u">drawdown</a></p>'
    (out,) = t.translate([src], {"trend following": "趋势跟踪", "drawdown": "回撤"})
    assert out == '<p>【译】趋势跟踪 and <a href="u">【译】回撤</a></p>'
    assert same_structure(src, out)
    assert t.translate([src], {}) == t.translate([src], {})  # 确定性
    assert t.chars_used == 3 * len(src)


def test_fake_translator_glossary_is_case_sensitive_like_deepl() -> None:
    t = FakeTranslator(prefix="")
    g = {"drawdown": "回撤"}
    assert t.translate(["<p>Drawdown</p>"], g) == ["<p>回撤</p>"]  # 句首大写：靠变体命中
    assert t.translate(["<p>DRAWDOWN</p>"], g) == ["<p>DRAWDOWN</p>"]  # 全大写：不命中


def test_fake_translator_quota_rejects_whole_batch() -> None:
    t = FakeTranslator(quota_chars=10)
    t.translate(["12345"], {})
    with pytest.raises(QuotaExceededError):
        t.translate(["123", "456"], {})
    assert t.chars_used == 5
    assert len(t.calls) == 1
