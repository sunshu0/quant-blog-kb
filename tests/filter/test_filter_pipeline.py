"""filter 阶段：L1、L2（含复用）、L3 覆盖、额度停下、评测。段落都是构造的句子。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from qbkb import store
from qbkb.filter import (
    Decision,
    FakeScorer,
    FilterConfig,
    FilterQuotaStopped,
    FilterStats,
    Override,
    SiteRules,
    evaluate,
    filter_article,
    run,
)
from qbkb.models import DiscoveredUrl, Score, Segment, Status, body_hash

BASE = "https://example.test/insights/"
CFG = FilterConfig(
    include_categories=("量化方法论", "基础概念"),
    exclude_categories=("宏观周评",),
    rubric_version="v1",
    threshold=7,
    sites={"example": SiteRules(("/insights/weekly/",), min_words=20)},
    excerpt_words=10,
)
LONG = "<p>" + " ".join(f"word{i}" for i in range(30)) + "</p>"
DISCLAIMER = "<p>" + " ".join("legal" for _ in range(50)) + "</p>"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def add(
    conn: sqlite3.Connection,
    slug: str,
    title: str = "Carry explained",
    paras: tuple[str, ...] = (LONG,),
    n_body: int | None = None,
) -> str:
    url = BASE + slug
    store.upsert_discovered(conn, DiscoveredUrl(url, "example", slug.replace("/", "-")))
    segs = [Segment(i, h) for i, h in enumerate(paras)]
    store.replace_segments(conn, url, segs)
    store.set_status(
        conn,
        url,
        Status.EXTRACTED,
        title=title,
        body_hash=body_hash(segs),
        body_segments=len(paras) if n_body is None else n_body,
    )
    return url


def row(conn: sqlite3.Connection, url: str) -> sqlite3.Row:
    r = store.get_article(conn, url)
    assert r is not None
    return r


class FixedScorer:
    """每次都给同一个分数；记录调用。"""

    def __init__(self, value: float, category: str, version: str = "v1@fixed") -> None:
        self.value, self.category, self._version = value, category, version
        self.calls: list[tuple[str, str]] = []

    @property
    def version(self) -> str:
        return self._version

    def score(self, title: str, excerpt: str) -> Score:
        self.calls.append((title, excerpt))
        return Score(self.value, self.category, True, "构造的理由", self._version)


def test_l1_excluded_prefix_and_short_body(conn: sqlite3.Connection) -> None:
    weekly = add(conn, "weekly/rates")
    short = add(conn, "short", paras=("<p>too short</p>",))
    scorer = FixedScorer(9, "量化方法论")
    stats = run(conn, "example", CFG, scorer)
    assert stats.skipped_l1 == 2 and scorer.calls == []
    assert row(conn, weekly)["status"] == Status.SKIPPED
    assert "/insights/weekly/" in row(conn, weekly)["score_reason"]
    assert "2 个单词" in row(conn, short)["score_reason"]


def test_word_count_ignores_appendix(conn: sqlite3.Connection) -> None:
    # 正文太短，后面的版权声明很长：版权声明不算字数
    url = add(conn, "video", paras=("<p>tiny body</p>", DISCLAIMER), n_body=1)
    run(conn, "example", CFG, FixedScorer(9, "量化方法论"))
    assert row(conn, url)["status"] == Status.SKIPPED


def test_l2_keep_and_skip_write_all_score_columns(conn: sqlite3.Connection) -> None:
    keep = add(conn, "keep")
    stats = run(conn, "example", CFG, FixedScorer(8, "基础概念"))
    assert stats.kept == 1
    r = row(conn, keep)
    assert r["status"] == Status.FILTERED
    assert (r["value_score"], r["category"], r["evergreen"]) == (8.0, "基础概念", 1)
    assert r["score_reason"] == "构造的理由" and r["rubric_version"] == "v1@fixed"
    assert r["scored_body_hash"] == r["body_hash"]

    low = add(conn, "low")
    macro = add(conn, "macro")
    run(conn, "example", CFG, FixedScorer(6, "量化方法论"), limit=1)
    assert row(conn, low)["status"] == Status.SKIPPED  # 分数不够
    run(conn, "example", CFG, FixedScorer(9, "宏观周评"))
    r = row(conn, macro)
    assert r["status"] == Status.SKIPPED and r["value_score"] == 9.0  # 类别不收


def test_excerpt_is_title_plus_first_words(conn: sqlite3.Connection) -> None:
    add(conn, "a", title="My title")
    scorer = FixedScorer(8, "量化方法论")
    run(conn, "example", CFG, scorer)
    ((title, excerpt),) = scorer.calls
    assert title == "My title"
    assert excerpt.split() == [f"word{i}" for i in range(10)]


def test_reuse_score_when_body_and_version_unchanged(conn: sqlite3.Connection) -> None:
    url = add(conn, "a")
    run(conn, "example", CFG, FixedScorer(8, "量化方法论"))
    # 原文 lastmod 变了、重新抽取得到同样的正文
    store.set_status(conn, url, Status.EXTRACTED)
    again = FixedScorer(1, "宏观周评")  # 同一个 version，不该被调用
    stats = run(conn, "example", CFG, again)
    assert again.calls == [] and stats.reused == 1
    assert row(conn, url)["status"] == Status.FILTERED


def test_rescore_when_body_or_rubric_changes(conn: sqlite3.Connection) -> None:
    url = add(conn, "a")
    run(conn, "example", CFG, FixedScorer(8, "量化方法论"))
    new_rubric = FixedScorer(3, "量化方法论", version="v2@fixed")
    store.set_status(conn, url, Status.EXTRACTED)
    run(conn, "example", CFG, new_rubric)
    assert len(new_rubric.calls) == 1 and row(conn, url)["status"] == Status.SKIPPED

    store.set_status(conn, url, Status.EXTRACTED, body_hash="changed")
    same_version = FixedScorer(9, "量化方法论", version="v2@fixed")
    run(conn, "example", CFG, same_version)
    assert len(same_version.calls) == 1 and row(conn, url)["scored_body_hash"] == "changed"


def test_override_beats_l1_and_l2(conn: sqlite3.Connection) -> None:
    weekly = add(conn, "weekly/history")
    good = add(conn, "good")
    overrides = {
        weekly: Override(Decision.INCLUDE, "基础概念", "讲了历史规律"),
        good: Override(Decision.EXCLUDE),
    }
    scorer = FixedScorer(9, "量化方法论")
    stats = run(conn, "example", CFG, scorer, overrides)
    assert stats.overridden == 2 and scorer.calls == []
    r = row(conn, weekly)
    assert r["status"] == Status.FILTERED and r["category"] == "基础概念"
    assert "人工覆盖" in r["score_reason"] and "讲了历史规律" in r["score_reason"]
    assert row(conn, good)["status"] == Status.SKIPPED


def test_new_override_reapplies_to_already_filtered(conn: sqlite3.Connection) -> None:
    url = add(conn, "a")
    run(conn, "example", CFG, FixedScorer(8, "量化方法论"))
    assert row(conn, url)["status"] == Status.FILTERED
    run(conn, "example", CFG, FixedScorer(8, "量化方法论"), {url: Override(Decision.EXCLUDE)})
    r = row(conn, url)
    assert r["status"] == Status.SKIPPED
    assert r["value_score"] == 8.0  # 打分列保留，去掉覆盖后还能复用


def test_refilter_reuses_scores_with_new_threshold(conn: sqlite3.Connection) -> None:
    url = add(conn, "a")
    run(conn, "example", CFG, FixedScorer(7.5, "量化方法论"))
    cfg8 = FilterConfig(
        CFG.include_categories, CFG.exclude_categories, "v1", 8, CFG.sites, CFG.excerpt_words
    )
    scorer = FixedScorer(1, "宏观周评")
    stats = run(conn, "example", cfg8, scorer, refilter=True)
    assert scorer.calls == [] and stats.reused == 1
    assert row(conn, url)["status"] == Status.SKIPPED


def test_quota_stops_and_keeps_progress(conn: sqlite3.Connection) -> None:
    urls = [add(conn, s) for s in ("a", "b", "c")]
    with pytest.raises(FilterQuotaStopped) as exc:
        run(conn, "example", CFG, FakeScorer(quota_calls=2))
    assert exc.value.stats.scored == 2
    assert [row(conn, u)["status"] for u in urls] == [
        Status.FILTERED,
        Status.FILTERED,
        Status.EXTRACTED,
    ]
    stats = run(conn, "example", CFG, FakeScorer())
    assert stats.scored == 1  # 只打剩下的那篇
    assert row(conn, urls[2])["status"] == Status.FILTERED


def test_bad_category_keeps_article_extracted(conn: sqlite3.Connection) -> None:
    url = add(conn, "a")
    stats = run(conn, "example", CFG, FixedScorer(9, "体育"))
    assert len(stats.errors) == 1
    r = row(conn, url)
    assert r["status"] == Status.EXTRACTED and "filter: " in r["error"]


def test_filter_article_unknown_url(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        filter_article(conn, BASE + "nope", CFG, FakeScorer(), {}, FilterStats())


def test_evaluate_agreement(conn: sqlite3.Connection) -> None:
    a, b, c = add(conn, "a"), add(conn, "b"), add(conn, "c")
    run(conn, "example", CFG, FixedScorer(8, "量化方法论"))
    store.set_status(conn, c, Status.SKIPPED, value_score=2.0, category="宏观周评")
    items = {
        a: Decision.INCLUDE,
        b: Decision.EXCLUDE,
        c: Decision.EXCLUDE,
        BASE + "missing": Decision.INCLUDE,
    }
    r = evaluate(conn, items, CFG)
    assert (r.total, r.compared, r.agree) == (4, 3, 2)
    assert r.agreement == pytest.approx(2 / 3)
    assert (r.true_pos, r.false_pos, r.false_neg, r.true_neg) == (1, 1, 0, 1)
    assert [d.url for d in r.disagreements] == [b]
    assert r.missing == [BASE + "missing"]
    only = evaluate(conn, items, CFG, rubric_version="v9")
    assert only.compared == 0 and only.agreement is None
