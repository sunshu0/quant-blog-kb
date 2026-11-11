"""PR #11 审查修复：recheck 不降级、成本提示、超长判断、脚注识别、restore 顺序。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Segment, Status, segment_hash
from qbkb.translate import FakeTranslator, Glossary, load_glossary, same_structure
from qbkb.translate.footnotes import SUP_MARK, protect_sups, repair_marks, restore_sups
from qbkb.translate.glossary import NO_TERMS, SEGMENT_RULES
from qbkb.translate.pipeline import translate_article
from qbkb.translate.structure import footnote_marks, numeric_sups

URL = "https://www.man.com/insights/r"
GOOD = "<p>Plain words here.</p>"
GOOD_ZH = "<p>普通的话。</p>"
FIXABLE = "<p>Across many signals<sup>3</sup>. Next we start.</p>"
FIXABLE_BAD = "<p>参考众多信号³<sup>3</sup>。下面开始。</p>"
FAR = "<p>The index fell 30% in 2001<sup>13</sup>, far more than peers.</p>"
FAR_BAD = "<p>该指数在2001年至20<sup>13</sup>年间下跌了30%¹³，远超同行。</p>"
STAMP = "2026-01-02T03:04:05+00:00"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(url=URL, site="man", slug="r"))
    yield c
    c.close()


def _setup(
    conn: sqlite3.Connection, status: Status, pairs: list[tuple[str, str]], g: Glossary
) -> None:
    store.replace_segments(conn, URL, [Segment(i, s) for i, (s, _) in enumerate(pairs)])
    for src, tgt in pairs:
        store.tm_put(conn, segment_hash(src), "fake", src, tgt, g.version_for(src))
    store.set_status(conn, URL, status, translator="fake", translated_at=STAMP)


def _row(conn: sqlite3.Connection) -> sqlite3.Row:
    row = store.get_article(conn, URL)
    assert row is not None
    return row


# ---------------------------------------------------------------- 1. recheck 不降级


def test_unchanged_published_article_keeps_status(conn: sqlite3.Connection) -> None:
    g = Glossary("1", {})
    _setup(conn, Status.PUBLISHED, [(GOOD, GOOD_ZH)], g)
    stats = translate_article(conn, URL, FakeTranslator(), g)
    assert stats.changed == 0 and stats.tm_hits == 1
    row = _row(conn)
    assert row["status"] == Status.PUBLISHED and row["translated_at"] == STAMP


def test_repaired_published_article_goes_back_to_translated(conn: sqlite3.Connection) -> None:
    g = Glossary("1", {})
    _setup(conn, Status.PUBLISHED, [(GOOD, GOOD_ZH), (FIXABLE, FIXABLE_BAD)], g)
    stats = translate_article(conn, URL, FakeTranslator(), g)
    assert stats.changed == 1
    row = _row(conn)
    assert row["status"] == Status.TRANSLATED and row["translated_at"] != STAMP


def test_unchanged_translated_article_keeps_timestamp(conn: sqlite3.Connection) -> None:
    g = Glossary("1", {})
    _setup(conn, Status.TRANSLATED, [(GOOD, GOOD_ZH)], g)
    translate_article(conn, URL, FakeTranslator(), g)
    row = _row(conn)
    assert row["status"] == Status.TRANSLATED and row["translated_at"] == STAMP


def test_needs_review_without_changes_keeps_timestamp(conn: sqlite3.Connection) -> None:
    g = Glossary("1", {})
    _setup(conn, Status.NEEDS_REVIEW, [(GOOD, GOOD_ZH), (FAR, FAR_BAD)], g)
    t = FakeTranslator()
    stats = translate_article(conn, URL, t, g)
    assert t.calls == [] and stats.rejected == [1] and stats.changed == 0
    row = _row(conn)
    assert row["status"] == Status.NEEDS_REVIEW and row["translated_at"] == STAMP


def test_needs_review_that_now_passes_goes_to_translated(conn: sqlite3.Connection) -> None:
    g = Glossary("1", {})
    _setup(conn, Status.NEEDS_REVIEW, [(FIXABLE, FIXABLE_BAD)], g)
    translate_article(conn, URL, FakeTranslator(), g)
    assert _row(conn)["status"] == Status.TRANSLATED


def test_published_article_with_a_bad_old_translation_goes_to_review(
    conn: sqlite3.Connection,
) -> None:
    g = Glossary("1", {})
    _setup(conn, Status.PUBLISHED, [(FAR, FAR_BAD)], g)
    translate_article(conn, URL, FakeTranslator(), g)
    assert _row(conn)["status"] == Status.NEEDS_REVIEW


def _cli_db(tmp_path: Path, status: Status, pairs: list[tuple[str, str]]) -> Path:
    db = tmp_path / "cli.db"
    c = store.connect(db)
    store.upsert_discovered(c, DiscoveredUrl(url=URL, site="man", slug="r"))
    _setup(c, status, pairs, load_glossary())
    c.close()
    return db


def test_cli_recheck_keeps_published_and_prints_the_cost(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 一段没变，一段含术语、但翻译记忆里是旧术语版本的译文：要重翻、花额度
    term = "<p>A drawdown happened.</p>"
    db = _cli_db(tmp_path, Status.PUBLISHED, [(GOOD, GOOD_ZH)])
    c = store.connect(db)
    store.replace_segments(c, URL, [Segment(0, GOOD), Segment(1, term)])
    store.tm_put(c, segment_hash(term), "fake", term, "<p>发生回撤。</p>", "g1-old")
    c.close()
    t = FakeTranslator()
    argv = ["translate", "--site", "man", "--db", str(db), "--translator", "fake", "--recheck"]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == 0
    out = capsys.readouterr().out
    assert f"预计约 {len(term)} 字符" in out and "--recheck：上面的" in out
    assert t.calls == [[term]]
    c = store.connect(db)
    assert store.get_article(c, URL)["status"] == Status.TRANSLATED  # type: ignore[index]
    c.close()
    # 再跑一次：什么都没变，已经 translated 的保持原样、不花额度
    t2 = FakeTranslator()
    assert cli.main(argv, deps=cli.Deps(translator=t2)) == 0
    assert t2.calls == [] and "预计约 0 字符" in capsys.readouterr().out


def test_cli_recheck_estimate_skips_rejected_without_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _cli_db(tmp_path, Status.NEEDS_REVIEW, [(GOOD, GOOD_ZH)])
    c = store.connect(db)
    store.replace_segments(c, URL, [Segment(0, GOOD), Segment(1, FAR)])
    g = load_glossary()
    store.tm_put(
        c, segment_hash(FAR), "fake", FAR, FAR_BAD, g.version_for(FAR), quality=store.TM_REJECTED
    )
    c.close()
    argv = ["translate", "--site", "man", "--db", str(db), "--translator", "fake", "--recheck"]
    assert cli.main(argv, deps=cli.Deps(translator=FakeTranslator())) == cli.EXIT_ATTENTION
    assert "预计约 0 字符" in capsys.readouterr().out
    t = FakeTranslator(prefix="")
    assert cli.main([*argv, "--retry-rejected"], deps=cli.Deps(translator=t)) == 0
    assert f"预计约 {len(FAR)} 字符" in capsys.readouterr().out


def test_recheck_help_mentions_cost(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["translate", "--help"])
    help_text = "".join(capsys.readouterr().out.split())  # argparse 会折行
    assert "会重翻" in help_text and "保持原状态" in help_text


# ---------------------------------------------------------------- 4. 超长按送出去的长度判断


def test_oversized_uses_length_after_placeholders(conn: sqlite3.Connection) -> None:
    src = "<p>" + "word " * 10 + "<sup>1</sup><sup>2</sup></p>"
    limit = len(src) + 5  # 原文没超，换成占位链接以后超了
    assert len(protect_sups(src)) > limit
    store.replace_segments(conn, URL, [Segment(0, src)])
    stats = translate_article(conn, URL, FakeTranslator(), Glossary("1", {}), max_batch_chars=limit)
    assert stats.oversized == [0]


# ---------------------------------------------------------------- 5、6. 脚注识别


def test_stray_number_only_checked_when_source_has_no_such_number() -> None:
    # 原文正文里本来就有 5：译文多出的 5 可能是正文数字，不按"复制的脚注号"拦
    src = "<p>We use 5 rules, as noted<sup>5</sup>.</p>"
    assert same_structure(src, "<p>我们用 5 条规则，如前所述<sup>5</sup>5。</p>")
    # 原文里 5 只作为脚注出现：译文段末多一个 5 就拦
    only = "<p>As noted before<sup>5</sup>.</p>"
    assert not same_structure(only, "<p>如前所述<sup>5</sup>，见注5。</p>")


def test_sup_with_spaces_is_recognised_the_same_everywhere() -> None:
    src = "<p>Some text<sup> 24 </sup> here.</p>"
    assert numeric_sups(src) == ["24"]
    got = "<p>一些文字²⁴<sup>24 </sup>在这里。</p>"
    fixed = repair_marks(src, got)
    assert fixed == "<p>一些文字<sup>24 </sup>在这里。</p>" and same_structure(src, fixed)


@pytest.mark.parametrize(
    "tgt",
    [
        "<p>甲<sup>1<sup>2</sup></sup></p>",  # 嵌套
        "<p>甲<sup>1</sup></sup>2</p>",  # 多一个结束标签
        "<p>甲<sup>1</p>",  # 没闭合
    ],
)
def test_unbalanced_sups_are_rejected(tgt: str) -> None:
    assert not same_structure("<p>A<sup>1</sup></p>", tgt)


def test_footnote_marks_use_the_parser() -> None:
    # 嵌套的 <sup> 不会让奇偶位错开、把正文里的 ² 漏掉
    assert footnote_marks("<p>x<sup>1<sup>2</sup></sup> y²</p>").count("2") == 2


# ---------------------------------------------------------------- 9. restore 先认记号


def test_restore_prefers_the_mark_over_digits() -> None:
    # DeepL 把正文数字 3 拉进了链接：记号才是脚注的位置，数字留在正文里
    assert restore_sups(f'<a href="#qbkb-sup-3">3{SUP_MARK}</a>') == "3<sup>3</sup>"
    assert restore_sups(f'<a href="#qbkb-sup-3">{SUP_MARK}3年</a>') == "<sup>3</sup>3年"


# ---------------------------------------------------------------- 7. 规则版本


def test_segment_rules_bumped_but_untermed_segments_still_hit() -> None:
    assert SEGMENT_RULES == "g2"
    g = Glossary("1", {"drawdown": "回撤"})
    assert g.version_for("<p>No terms.</p>") == NO_TERMS == "g1-none"
    assert g.version_for("<p>A drawdown.</p>").startswith("g2-")


# ---------------------------------------------------------------- 用户确认的术语表决定


def test_confirmed_glossary_decisions() -> None:
    g = load_glossary()
    assert g["multiples"] == "估值倍数" and "multiple" not in g
    assert g["breakeven inflation"] == "盈亏平衡通胀率" and "breakeven" not in g
    assert "collar" not in g and "PE" not in g and "Introduction" not in g
    assert g["private equity"] == "私募股权"
    assert g["Exhibit 3"] == "图表3" and "Exhibit" not in g
    assert g.terms_in("<p>See the exhibit hall.</p>") == {}
    assert set(g.terms_in("<p>As Exhibit 12 shows.</p>")) == {"Exhibit 12"}


def test_numbered_terms_are_not_annotated() -> None:
    from qbkb.export.markdown import TermAnnotator

    a = TermAnnotator()
    out = a.annotate("<p>如图表3所示的回撤</p>", {"Exhibit 3": "图表3", "drawdown": "回撤"})
    assert out == "<p>如图表3所示的回撤（drawdown）</p>"
