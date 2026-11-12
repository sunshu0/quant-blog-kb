"""按栏目推进状态、新栏目提醒、抽取时判定栏目、v1 老数据的迁移。"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

from sections_site import BASE, SECTIONS, make_site, page

from qbkb import extract, store
from qbkb.extract.meta import clean_author
from qbkb.models import DiscoveredUrl, Segment, Status, body_hash
from qbkb.sections import apply_sections, new_sections, translatable
from qbkb.store.db import SCHEMA_VERSION


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return store.connect(tmp_path / "state.db")


def _add(conn: sqlite3.Connection, path: str) -> str:
    url = BASE + path
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", path.rsplit("/", 1)[-1]))
    return url


def _fetched(conn: sqlite3.Connection, tmp_path: Path, path: str, html: str) -> str:
    url = _add(conn, path)
    raw = tmp_path / "raw" / (path.rsplit("/", 1)[-1] + ".html")
    raw.parent.mkdir(exist_ok=True)
    raw.write_text(html, encoding="utf-8")
    store.set_status(conn, url, Status.FETCHED, raw_path=str(raw), http_status=200)
    return url


def _status(conn: sqlite3.Connection, url: str) -> str:
    row = store.get_article(conn, url)
    assert row is not None
    return str(row["status"])


def test_discover_prefix_section_not_collected_is_not_fetched(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    site = make_site(SECTIONS)
    macro = _add(conn, "/insights/macro-blog/rates")
    plain = _add(conn, "/insights/carry")
    st = apply_sections(conn, site)
    assert st.unselected == 1
    assert _status(conn, macro) == Status.UNSELECTED
    assert _status(conn, plain) == Status.DISCOVERED  # 要抓到页面才知道栏目
    row = store.get_article(conn, macro)
    assert row is not None and row["section"] == "macro"


def test_extract_classifies_and_marks_unselected(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    site = make_site(SECTIONS)
    a = _fetched(conn, tmp_path, "/insights/carry", page("Carry", "ARTICLE | 5 MIN"))
    w = _fetched(conn, tmp_path, "/insights/weekly-1", page("W", "ARTICLE | 3 MIN | WEEKLY NOTES"))
    n = _fetched(conn, tmp_path, "/insights/pod-1", page("P", "PODCAST | 30 MIN | NEW SHOW"))
    short = _fetched(conn, tmp_path, "/insights/tiny", page("Tiny", "ARTICLE | 1 MIN", words=3))
    s = extract.run(conn, site, tmp_path)
    assert s.extracted == 4 and s.failed == 0
    assert _status(conn, a) == Status.EXTRACTED
    assert _status(conn, short) == Status.EXTRACTED  # 正文太短只标记，不跳过（C2）
    assert any("正文太短" in i for i in s.flagged[short])
    assert _status(conn, w) == Status.UNSELECTED
    assert _status(conn, n) == Status.UNSELECTED  # 新栏目：只记录，不采集
    row = store.get_article(conn, n)
    assert row is not None and row["section_key"] == "PODCAST | NEW SHOW" and row["section"] is None
    assert [(x.key, x.count) for x in s.new] == [("PODCAST | NEW SHOW", 1)]
    assert sorted(translatable(conn, site)) == sorted([a, short])


def test_cross_check_issue_goes_to_qc_issues(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    url = _fetched(
        conn, tmp_path, "/insights/x", page("X", "ARTICLE | 5 MIN", content_type="Video")
    )
    extract.run(conn, make_site(SECTIONS), tmp_path)
    row = store.get_article(conn, url)
    assert row is not None and row["status"] == Status.EXTRACTED
    issues = json.loads(row["qc_issues"])
    assert any("dataLayer contentType 是 Video" in i for i in issues)


def test_toggling_collect_moves_articles_back(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    w = _fetched(conn, tmp_path, "/insights/weekly-1", page("W", "ARTICLE | 3 MIN | WEEKLY NOTES"))
    macro = _add(conn, "/insights/macro-blog/rates")
    extract.run(conn, make_site(SECTIONS), tmp_path)
    assert _status(conn, w) == Status.UNSELECTED and _status(conn, macro) == Status.UNSELECTED
    picked = copy.deepcopy(SECTIONS)
    for sec in picked["list"]:
        sec["collect"] = True
    st = apply_sections(conn, make_site(picked))
    assert st.reselected == 2
    assert _status(conn, w) == Status.EXTRACTED  # 抽取过的不重抓
    assert _status(conn, macro) == Status.DISCOVERED  # 没抓过的回去抓
    # 再取消：又回到未选栏目
    apply_sections(conn, make_site(SECTIONS))
    assert _status(conn, w) == Status.UNSELECTED


def test_downstream_statuses_are_never_demoted(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    url = _add(conn, "/insights/macro-blog/old")
    store.set_status(conn, url, Status.PUBLISHED)
    apply_sections(conn, make_site(SECTIONS))
    row = store.get_article(conn, url)
    assert row is not None and row["status"] == Status.PUBLISHED and row["section"] == "macro"


def test_site_without_sections_collects_everything(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    site = make_site(None)
    url = _fetched(conn, tmp_path, "/insights/x", page("X", "PODCAST | 9 MIN | ANY"))
    s = extract.run(conn, site, tmp_path)
    assert s.new == [] and _status(conn, url) == Status.EXTRACTED
    assert translatable(conn, site) == [url]


def test_new_sections_counts_and_examples(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    for i in range(3):
        _fetched(conn, tmp_path, f"/insights/s{i}", page("S", "ARTICLE | 2 MIN | NEW SERIES"))
    _fetched(conn, tmp_path, "/insights/none", page("N", None))
    s = extract.run(conn, make_site(SECTIONS), tmp_path)
    assert [(n.key, n.count) for n in s.new] == [("ARTICLE | NEW SERIES", 3), ("", 1)]
    assert s.new[1].label == "（页面上读不到栏目信息）"
    assert new_sections(conn, make_site(SECTIONS)) == s.new


def _legacy(
    conn: sqlite3.Connection, tmp_path: Path, path: str, status: Status, tagline: str
) -> str:
    """模拟 v1 留下的数据：抽取过、有打分，section_key 为 NULL。"""
    url = _fetched(conn, tmp_path, path, page("Old", tagline))
    segs = [Segment(0, "<p>old body</p>")]
    store.replace_segments(conn, url, segs)
    store.set_status(
        conn, url, status, body_hash=body_hash(segs), value_score=3.0, category="宏观周评"
    )
    return url


def test_legacy_filtered_and_skipped_reenter_flow(tmp_path: Path) -> None:
    """迁移策略：v1 的 extracted / filtered / skipped 重新抽取，按 v2 的栏目规则进入流程。"""
    conn = _conn(tmp_path)
    site = make_site(SECTIONS)
    skipped = _legacy(conn, tmp_path, "/insights/a", Status.SKIPPED, "ARTICLE | 5 MIN")
    filtered = _legacy(
        conn, tmp_path, "/insights/b", Status.FILTERED, "ARTICLE | 3 MIN | WEEKLY NOTES"
    )
    # 还没重新抽取之前：老数据不翻译（不知道栏目）
    assert translatable(conn, site) == []
    s = extract.run(conn, site, tmp_path)
    assert s.legacy == 2
    assert _status(conn, skipped) == Status.EXTRACTED  # L2 筛掉的，栏目采集 → 回到流程
    assert _status(conn, filtered) == Status.UNSELECTED  # L2 收的，但栏目不采集
    row = store.get_article(conn, skipped)
    assert row is not None and row["value_score"] == 3.0  # 打分记录保留，不再起作用
    assert translatable(conn, site) == [skipped]
    # 再跑一次不会重复抽取
    assert extract.run(conn, site, tmp_path).legacy == 0


def test_legacy_published_only_backfills_section(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    url = _legacy(conn, tmp_path, "/insights/p", Status.PUBLISHED, "ARTICLE | 3 MIN | WEEKLY NOTES")
    before = store.get_article(conn, url)
    s = extract.run(conn, make_site(SECTIONS), tmp_path)
    after = store.get_article(conn, url)
    assert s.backfilled == 1 and s.legacy == 0
    assert before is not None and after is not None
    assert after["status"] == Status.PUBLISHED and after["section"] == "weekly"
    assert after["updated_at"] == before["updated_at"] and after["body_hash"] == before["body_hash"]


def test_legacy_without_raw_is_left_alone(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    url = _legacy(conn, tmp_path, "/insights/gone", Status.SKIPPED, "ARTICLE | 5 MIN")
    row = store.get_article(conn, url)
    assert row is not None
    Path(row["raw_path"]).unlink()
    s = extract.run(conn, make_site(SECTIONS), tmp_path)
    assert s.legacy == 0 and s.failed == 0 and _status(conn, url) == Status.SKIPPED


def test_author_fallback_and_heading_cleanup(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    html = page("Blog", None).replace(
        '<div id="article-authors"><a href="/p"><span class="fw-600">Test Author</span></a></div>',
        '<div id="blog-author-grid"><h3>About the author</h3><div class="team-card">'
        "<h4> Jane Example </h4><span>Strategist, Example Co</span></div></div>",
    )
    url = _fetched(conn, tmp_path, "/insights/macro-blog/post", html)
    extract.run(conn, make_site(SECTIONS), tmp_path)
    row = store.get_article(conn, url)
    assert row is not None and row["author"] == "Jane Example"
    assert clean_author("About the author Jane Example") == "Jane Example"
    assert clean_author("By: Jane Example") == "Jane Example"
    assert clean_author("Byron Example") == "Byron Example"


def test_store_upgrades_v2_db_with_section_columns(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE articles (url TEXT PRIMARY KEY, site TEXT NOT NULL, slug TEXT NOT NULL,"
        " status TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO articles VALUES ('u', 'ex', 's', 'skipped', 't')")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    conn = store.connect(db)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    assert {"section", "section_key"} <= cols
    row = store.get_article(conn, "u")
    assert row is not None and row["status"] == "skipped" and row["section_key"] is None
    assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
