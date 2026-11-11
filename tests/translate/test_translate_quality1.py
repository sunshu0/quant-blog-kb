"""PR A（译文质量修复）：D7 校验分级、脚注重复 / 错位、术语表短语、固定译文、说话人标签、按行翻译。

形状都来自第一批 50 篇的真实运行和 7 篇人工验收，句子是自己编的。
"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import deepl
import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, Segment, Status, segment_hash
from qbkb.translate import (
    DeepLTranslator,
    FakeTranslator,
    Glossary,
    estimate_chars,
    load_glossary,
    lookup_translation,
    same_structure,
    translate_article,
)
from qbkb.translate.fixed import is_label_shape, load_fixed, speaker_labels
from qbkb.translate.footnotes import SUP_MARK, protect_sups, repair_marks, restore_sups
from qbkb.translate.lines import split_lines
from qbkb.translate.structure import footnote_marks

# ---------------------------------------------------------------- D7 分级


def test_relaxed_emphasis_count_is_not_compared() -> None:
    src = "<p>The <em>first</em> rule and the <em>second</em> rule.</p>"
    assert same_structure(src, "<p>第一条和第二条<em>规则</em>。</p>")  # 合并
    assert same_structure("<p>A <em>long phrase</em>.</p>", "<p><em>长</em>的<em>短语</em>。</p>")
    assert same_structure("<p><strong>Bold</strong> text</p>", "<p>没有加粗的文字</p>")  # 丢了
    assert same_structure("<p><b><i>x</i></b></p>", "<p><i>x</i></p>")
    # 强调标签没正确闭合仍然算错（会吞掉后面的文字）
    assert not same_structure("<p><em>x</em> y</p>", "<p><em>x y</p>")


def test_links_compared_by_address_set() -> None:
    src = '<p>See <a href="https://example.test/paper">the full paper here</a>.</p>'
    split = (
        '<p>请看<a href="https://example.test/paper">全文</a>，'
        '<a href="https://example.test/paper">在这里</a>。</p>'
    )
    assert same_structure(src, split)  # 同一个地址拆成两段
    assert not same_structure(src, "<p>请看全文。</p>")  # 少了地址
    other = '<p>请看<a href="https://example.test/other">全文</a>。</p>'
    assert not same_structure(src, other)  # 地址变了
    extra = split.replace("</p>", '<a href="https://example.test/x">x</a></p>')
    assert not same_structure(src, extra)  # 多了地址


@pytest.mark.parametrize(
    ("src", "tgt"),
    [
        ('<p>x <img src="a.png"></p>', '<p>x <img src="b.png"></p>'),
        ("<p>run <code>f(x)</code></p>", "<p>运行 f(x)</p>"),
        ("<pre><code>x = 1</code></pre>", "<p>x = 1</p>"),
        ("<p><math><mi>x</mi></math></p>", "<p>x</p>"),
        (
            "<table><tr><td>a</td><td>b</td></tr></table>",
            "<table><tr><td>甲</td></tr><tr><td>乙</td></tr></table>",
        ),
    ],
)
def test_strict_tags(src: str, tgt: str) -> None:
    assert not same_structure(src, tgt)


def test_footnote_lost_or_duplicated() -> None:
    src = "<p>Returns rose sharply<sup>3</sup>, as expected.</p>"
    assert same_structure(src, "<p>回报大幅上升<sup>3</sup>，正如预期。</p>")
    assert not same_structure(src, "<p>回报大幅上升，正如预期。</p>")  # 丢了
    assert not same_structure(src, "<p>回报大幅上升<sup>3</sup><sup>3</sup>，正如预期。</p>")
    # 验收发现的形状：DeepL 另写了一个 Unicode 上标
    assert footnote_marks("<p>上升³<sup>3</sup>。</p>") == ["3", "3"]
    assert not same_structure(src, "<p>回报大幅上升³<sup>3</sup>，正如预期。</p>")
    assert not same_structure(src, "<p>回报大幅上升<sup>3</sup>，正如预期³。</p>")


def test_footnote_number_copied_into_text() -> None:
    # 小标题末尾的脚注被插进句中，末尾多一个裸数字
    src = "<h2>2. Splitting Prices into Parts<sup>5</sup></h2>"
    assert same_structure(src, "<h2>2. 把价格拆成<sup>5</sup>几部分</h2>")
    assert not same_structure(src, "<h2>2. 把<sup>5</sup>价格拆成几部分5</h2>")
    assert not same_structure(src, "<h2>2. 把价格拆成几部分5<sup>5</sup></h2>")
    # 原文本来就有的数字不算
    body = "<p>It grew by 3<sup>3</sup> last year.</p>"
    assert same_structure(body, "<p>去年增长了3<sup>3</sup>。</p>")
    assert same_structure(
        "<p>We use 5 rules<sup>5</sup>.</p>", "<p>我们用 5 条规则<sup>5</sup>。</p>"
    )


# ---------------------------------------------------------------- 脚注占位


def test_placeholder_has_no_digits() -> None:
    sent = protect_sups("<p>A fall of 30% in 2001<sup>13</sup> was rare.</p>")
    assert sent == f'<p>A fall of 30% in 2001<a href="#qbkb-sup-13">{SUP_MARK}</a> was rare.</p>'
    # 链接文字里没有 13，DeepL 不会把 "2001" 和 "13" 连读成年份区间


def test_restore_new_and_legacy_placeholders() -> None:
    assert (
        restore_sups(f'<p>下跌<a href="#qbkb-sup-13">{SUP_MARK}</a>。</p>')
        == "<p>下跌<sup>13</sup>。</p>"
    )
    assert restore_sups(f'<a href="#qbkb-sup-2">率{SUP_MARK}</a>') == "率<sup>2</sup>"
    assert restore_sups('<a href="#qbkb-sup-2"></a>') == "<sup>2</sup>"
    assert restore_sups('<a href="#qbkb-sup-2">2</a>') == "<sup>2</sup>"  # PR #8 的写法
    # DeepL 把记号挪到了链接外面：原文没有 † 就去掉
    assert restore_sups(f'<p>甲{SUP_MARK}<a href="#qbkb-sup-2"></a></p>') == "<p>甲<sup>2</sup></p>"
    kept = restore_sups(f"<p>甲{SUP_MARK}</p>", keep_marks=True)
    assert kept == f"<p>甲{SUP_MARK}</p>"


@pytest.mark.parametrize(
    ("got", "fixed"),
    [
        ("<p>很多信号³<sup>3</sup>。</p>", "<p>很多信号<sup>3</sup>。</p>"),
        ("<p>增加了20%。¹<sup>1</sup> 此后</p>", "<p>增加了20%。<sup>1</sup> 此后</p>"),
        ("<p>篮子¹⁵，<sup>15</sup>用来</p>", "<p>篮子<sup>15</sup>，用来</p>"),
        ("<p>篮子<sup>15</sup>¹⁵用来</p>", "<p>篮子<sup>15</sup>用来</p>"),
    ],
)
def test_repair_adjacent_unicode_duplicates(got: str, fixed: str) -> None:
    n = fixed.split("<sup>")[1].split("</sup>")[0]
    src = f"<p>Some words here<sup>{n}</sup> and more words.</p>"
    assert not same_structure(src, got)
    assert repair_marks(src, got) == fixed
    assert same_structure(src, fixed)


def test_repair_converts_lonely_unicode_mark() -> None:
    src = "<p>However much leverage<sup>24 </sup>it takes.</p>"
    out = repair_marks(src, "<p>无论用多少借款²⁴。</p>")
    assert out == "<p>无论用多少借款<sup>24</sup>。</p>" and same_structure(src, out)


def test_repair_leaves_ambiguous_shapes_for_review() -> None:
    # "2001 年 + 脚注 13" 被读成年份区间，又在句末补了 ¹³：离得远，不猜
    src = "<p>The index fell 30% in 2001<sup>13</sup>, far more than peers.</p>"
    got = "<p>该指数在2001年至20<sup>13</sup>年间下跌了30%¹³，远超同行。</p>"
    assert repair_marks(src, got) == got and not same_structure(src, got)
    # 原文自己有 Unicode 上标时不动
    assert (
        repair_marks("<p>10 m² area<sup>1</sup></p>", "<p>10 m² 面积¹</p>") == "<p>10 m² 面积¹</p>"
    )


class ScriptedClient:
    """假的 deepl 客户端：按 rewrite 改写送来的每条文本，记录请求。"""

    def __init__(self, rewrite: Any = None) -> None:
        self.rewrite = rewrite or (lambda t: t)
        self.calls: list[list[str]] = []

    def translate_text(self, text: Any, **kwargs: Any) -> Any:
        texts = list(text)
        self.calls.append(texts)
        return [deepl.TextResult(self.rewrite(t), "EN", None) for t in texts]

    def list_glossaries(self) -> list[deepl.GlossaryInfo]:
        return []

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo:
        raise AssertionError("空术语表不应该创建")

    def get_usage(self) -> deepl.Usage:
        return deepl.Usage({"character_count": 0, "character_limit": 1_000_000})


def test_deepl_backend_repairs_unicode_duplicates() -> None:
    src = "<p>Across many signals<sup>3</sup>. Next we start.</p>"
    # 模拟 DeepL 在占位链接前另写一个 ³
    client = ScriptedClient(lambda t: t.replace("<a ", "³<a "))
    (out,) = DeepLTranslator(client).translate([src], {})
    assert out == src
    assert "³" not in out and same_structure(src, out)


# ---------------------------------------------------------------- 按行翻译


def test_split_numbered_footnote_list() -> None:
    html = "<p><cite>1. First note, 2019.<br> 2. Second note: “A Title?”<br> 3. Third.</cite></p>"
    plan = split_lines(html)
    assert plan is not None
    assert plan.lines == [
        "<p><cite>1. First note, 2019.</cite></p>",
        "<p><cite>2. Second note: “A Title?”</cite></p>",
        "<p><cite>3. Third.</cite></p>",
    ]
    zh = ["<p><cite>1. 第一条，2019。</cite></p>", "<p><cite>2. 第二条：《标题？》</cite></p>", "x"]
    assert plan.join(zh) == "<p><cite>1. 第一条，2019。<br> 2. 第二条：《标题？》<br> x</cite></p>"


def test_split_rules() -> None:
    poem = "<p>I keep a little list;<br> it tells me what to do;<br> and when.</p>"
    assert split_lines(poem) is not None
    two_numbered = "<p><cite>1. As of May.<br> 2. See the data.</cite></p>"
    assert split_lines(two_numbered) is not None
    assert split_lines("<p>One sentence that wraps,<br> once.</p>") is None  # 句中一个换行不拆
    assert split_lines("<p><em>a<br>b<br>c</em> d</p>") is None  # <br> 在 em 里面
    nested = "<p><cite><b>Index</b><br> <b>Name A</b><br> text</cite></p>"
    plan = split_lines(nested)
    assert plan is not None and plan.prefix == "<p><cite>"
    assert split_lines("<p>no breaks</p>") is None


def test_deepl_backend_sends_lines_separately() -> None:
    poem = "<p>Line one “here<br> and” line two;<br> line three.</p>"
    client = ScriptedClient()
    out, plain = DeepLTranslator(client).translate([poem, "<p>Plain.</p>"], {})
    assert plain == "<p>Plain.</p>"
    assert client.calls == [
        [
            "<p>Line one “here</p>",
            "<p>and” line two;</p>",
            "<p>line three.</p>",
            "<p>Plain.</p>",
        ]
    ]
    assert out == poem and same_structure(poem, out)


# ---------------------------------------------------------------- 术语表


def test_repo_glossary_phrases_only() -> None:
    g = load_glossary()
    assert g.terms_in("<p>We carry over the spare cash into next year.</p>") == {}
    assert "carry trade" in g.terms_in("<p>The carry trade lost money.</p>")
    assert g.terms_in("<p>Many factors matter here.</p>") == {}
    assert "value factor" in g.terms_in("<p>The value factor recovered.</p>")
    assert g["disinflation"] == "通胀回落" and g["portable alpha"] == "可转移 alpha"


def test_longest_term_wins() -> None:
    g = Glossary("1", {"volatility": "波动率", "implied volatility": "隐含波动率"})
    assert g.terms_in("<p>Implied volatility rose.</p>") == {"implied volatility": "隐含波动率"}
    both = g.terms_in("<p>Implied volatility rose; realised volatility fell.</p>")
    assert set(both) == {"implied volatility", "volatility"}


def test_acronyms_match_case_sensitively_with_right_boundary() -> None:
    g = Glossary("1", {"CLO": "CLO", "ROE": "ROE"})
    assert g.terms_in("<p>Close the door and roam.</p>") == {}
    assert set(g.terms_in("<p>CLOs and ROE.</p>")) == {"CLO", "ROE"}


# ---------------------------------------------------------------- 固定译文、说话人标签

URL = "https://example.test/insights/q"
NOTICE = "<p>These notes are for teaching only.</p>"
NOTICE_ZH = "<p>本笔记仅用于教学。</p>"
BODY = "<p>A drawdown is a fall from a peak.</p>"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(url=URL, site="example", slug="q"))
    yield c
    c.close()


def _fixed_file(tmp_path: Path, zh: str = NOTICE_ZH) -> Path:
    p = tmp_path / "fixed_translations.yaml"
    p.write_text(
        f"entries:\n  {segment_hash(NOTICE)}:\n    note: test\n    zh: {zh}\n", encoding="utf-8"
    )
    return p


def test_load_fixed_validates(tmp_path: Path) -> None:
    assert len(load_fixed(tmp_path / "missing.yaml")) == 0
    assert load_fixed(_fixed_file(tmp_path))[segment_hash(NOTICE)] == NOTICE_ZH
    bad = tmp_path / "bad.yaml"
    bad.write_text("entries:\n  abc:\n    zh: x\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixed(bad)
    bad.write_text(f"entries:\n  {segment_hash(NOTICE)}:\n    note: no zh\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_fixed(bad)


def test_glossary_loads_sibling_fixed_file(tmp_path: Path) -> None:
    _fixed_file(tmp_path)
    (tmp_path / "glossary.yaml").write_text("version: 1\nterms: {}\n", encoding="utf-8")
    assert segment_hash(NOTICE) in load_glossary(tmp_path / "glossary.yaml").fixed


def test_fixed_translation_is_not_sent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    g = Glossary("1", {"drawdown": "回撤"}, load_fixed(_fixed_file(tmp_path)))
    store.replace_segments(conn, URL, [Segment(0, BODY), Segment(1, NOTICE)])
    # 以前 DeepL 的译文还在翻译记忆里：固定译文优先
    store.tm_put(conn, segment_hash(NOTICE), "fake", NOTICE, "<p>旧译文</p>", g.version_for(NOTICE))
    assert estimate_chars(conn, [URL], "fake", g) == len(BODY)
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, g)
    assert t.calls == [[BODY]] and stats.fixed == 1 and stats.chars == len(BODY)
    assert lookup_translation(conn, NOTICE, "fake", g) == NOTICE_ZH
    assert store.tm_get(conn, segment_hash(NOTICE), "fake", g.version_for(NOTICE)) == NOTICE_ZH


def test_fixed_translation_with_wrong_structure_is_an_error(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    g = Glossary("1", {}, load_fixed(_fixed_file(tmp_path, zh='<p><a href="x">错</a></p>')))
    store.replace_segments(conn, URL, [Segment(0, NOTICE)])
    with pytest.raises(ValueError, match="固定译文"):
        translate_article(conn, URL, FakeTranslator(), g)


def test_speaker_labels() -> None:
    assert is_label_shape("<p><b>Ann Smith:</b></p>")
    assert is_label_shape("<p><strong>Ann Smith :</strong></p>")
    assert not is_label_shape("<p><b>Ann Smith</b></p>")  # 没有冒号
    assert not is_label_shape("<p><b>the summary:</b></p>")
    assert not is_label_shape("<p><b>Ann Smith:</b> hello</p>")
    segs = [
        Segment(0, "<p><b>Ann Smith:</b></p>"),
        Segment(1, "<p>Hello there.</p>"),
        Segment(2, "<p><b>Key Points:</b></p>"),  # 只出现一次，照常翻译
        Segment(3, "<p><b>Ann Smith:</b></p>"),
    ]
    assert speaker_labels(segs) == {segs[0].src_hash}


def test_speaker_labels_are_kept_in_english(conn: sqlite3.Connection) -> None:
    label = "<p><b>Ann Smith:</b></p>"
    store.replace_segments(
        conn,
        URL,
        [Segment(0, label), Segment(1, "<p>Hello.</p>"), Segment(2, label), Segment(3, BODY)],
    )
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, Glossary("1", {}))
    assert t.calls == [["<p>Hello.</p>", BODY]] and stats.speaker_labels == 1
    assert lookup_translation(conn, label, "fake", Glossary("1", {})) == label


# ---------------------------------------------------------------- 翻译记忆按新规则重新判断


def _status(conn: sqlite3.Connection) -> str:
    row = store.get_article(conn, URL)
    assert row is not None
    return str(row["status"])


def test_rejected_entry_that_passes_d7_is_reused_for_free(conn: sqlite3.Connection) -> None:
    src = "<p>The <em>first</em> and <em>second</em> idea.</p>"
    store.replace_segments(conn, URL, [Segment(0, src)])
    g = Glossary("1", {})
    tgt = "<p>第一个和第二个<em>想法</em>。</p>"  # 旧规则下 em 数量不同被拦
    store.tm_put(
        conn, segment_hash(src), "fake", src, tgt, g.version_for(src), quality=store.TM_REJECTED
    )
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, g)
    assert t.calls == [] and stats.revalidated == 1 and _status(conn) == Status.TRANSLATED
    entry = store.tm_lookup(conn, segment_hash(src), "fake", g.version_for(src))
    assert entry is not None and entry.quality == store.TM_OK


def test_ok_entry_with_duplicate_mark_is_repaired_or_sent_to_review(
    conn: sqlite3.Connection,
) -> None:
    fixable = "<p>Across many signals<sup>3</sup>. Next we start.</p>"
    far = "<p>The index fell 30% in 2001<sup>13</sup>, far more than peers.</p>"
    store.replace_segments(conn, URL, [Segment(0, fixable), Segment(1, far)])
    g = Glossary("1", {})
    for src, tgt in (
        (fixable, "<p>参考众多信号³<sup>3</sup>。下面开始。</p>"),
        (far, "<p>该指数在2001年至20<sup>13</sup>年间下跌了30%¹³，远超同行。</p>"),
    ):
        store.tm_put(conn, segment_hash(src), "fake", src, tgt, g.version_for(src))
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, g)
    assert t.calls == []  # 没有 --retry-rejected 不重翻
    assert stats.revalidated == 1 and stats.rejected == [1]
    assert _status(conn) == Status.NEEDS_REVIEW
    assert (
        lookup_translation(conn, fixable, "fake", g)
        == "<p>参考众多信号<sup>3</sup>。下面开始。</p>"
    )


def test_cli_recheck_revisits_published_articles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from qbkb import cli

    db = tmp_path / "state.db"
    c = store.connect(db)
    url = "https://www.man.com/insights/q"
    src = "<p>Across many signals<sup>3</sup>. Next we start.</p>"
    store.upsert_discovered(c, DiscoveredUrl(url=url, site="man", slug="q"))
    store.replace_segments(c, url, [Segment(0, src)])
    store.set_status(c, url, Status.PUBLISHED, translator="fake")
    g = load_glossary()
    bad = "<p>参考众多信号³<sup>3</sup>。下面开始。</p>"
    store.tm_put(c, segment_hash(src), "fake", src, bad, g.version_for(src))
    c.close()
    t = FakeTranslator()
    argv = ["translate", "--site", "man", "--db", str(db), "--translator", "fake"]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == 0
    assert "没有待翻译的文章" in capsys.readouterr().out  # 不加 --recheck 不碰已发布的
    assert cli.main([*argv, "--recheck"], deps=cli.Deps(translator=t)) == 0
    assert "按新规则复用 1 段" in capsys.readouterr().out
    assert t.calls == []  # 就地修好，不花额度
    c = store.connect(db)
    row = store.get_article(c, url)
    assert row is not None and row["status"] == Status.TRANSLATED  # 下次 export 重新生成
    assert lookup_translation(c, src, "fake", g) == "<p>参考众多信号<sup>3</sup>。下面开始。</p>"
    c.close()
