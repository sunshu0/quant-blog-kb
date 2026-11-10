"""共享 PR：老库迁移、翻译记忆质量标记、record_failure 的 http_status、新列。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, Segment, Status

URL = "https://example.test/insights/a"

# Phase 0 / PR #3 时期的表结构（没有 last_modified、qc_issues、quality 列）
OLD_SCHEMA = """
CREATE TABLE articles (
  url TEXT PRIMARY KEY, site TEXT NOT NULL, slug TEXT NOT NULL, status TEXT NOT NULL,
  lastmod TEXT, etag TEXT, http_status INTEGER, raw_path TEXT, body_hash TEXT,
  title TEXT, title_zh TEXT, author TEXT, published TEXT, tags TEXT, category TEXT,
  value_score REAL, evergreen INTEGER, score_reason TEXT, rubric_version TEXT,
  scored_body_hash TEXT, translator TEXT, translated_at TEXT, md_path TEXT, error TEXT,
  retries INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE translation_memory (
  src_hash TEXT NOT NULL, backend TEXT NOT NULL, src_html TEXT NOT NULL,
  tgt_html TEXT NOT NULL, glossary_version TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (src_hash, backend)
);
INSERT INTO translation_memory VALUES ('h-ok', 'fake', '<p>a</p>', '<p>甲</p>', 'v', 't');
INSERT INTO translation_memory VALUES ('h-bad', 'fake#rejected', '<p>b</p>', '<b>乙</b>', 'v', 't');
-- 同一段落既有合格译文、又有隔离区里的旧坏译文：合格的那条要保留
INSERT INTO translation_memory VALUES ('h-both', 'fake', '<p>c</p>', '<p>丙</p>', 'v', 't');
INSERT INTO translation_memory VALUES
  ('h-both', 'fake#rejected', '<p>c</p>', '<b>丙</b>', 'v', 't');
"""


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(URL, "example", "a", "2026-01-01"))
    yield c
    c.close()


def test_old_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.close()

    conn = store.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    assert {"last_modified", "qc_issues"} <= cols
    assert store.tm_get(conn, "h-ok", "fake", "v") == "<p>甲</p>"
    bad = store.tm_lookup(conn, "h-bad", "fake", "v")
    assert bad == store.TmEntry("<b>乙</b>", store.TM_REJECTED)
    assert store.tm_get(conn, "h-bad", "fake", "v") is None
    assert store.tm_lookup(conn, "h-both", "fake", "v") == store.TmEntry("<p>丙</p>", store.TM_OK)
    left = conn.execute("SELECT COUNT(*) FROM translation_memory WHERE backend LIKE '%#rejected'")
    assert left.fetchone()[0] == 0
    conn.close()
    # 再跑一次迁移不出错、不改数据
    conn = store.connect(path)
    assert store.tm_get(conn, "h-ok", "fake", "v") == "<p>甲</p>"
    conn.close()


def test_rejected_translation_is_hidden_from_tm_get(conn: sqlite3.Connection) -> None:
    h = Segment(0, "<p>x</p>").src_hash
    store.tm_put(conn, h, "fake", "<p>x</p>", "<b>坏</b>", "v", quality=store.TM_REJECTED)
    assert store.tm_get(conn, h, "fake", "v") is None
    assert store.tm_lookup(conn, h, "fake", "v") == store.TmEntry("<b>坏</b>", store.TM_REJECTED)
    # 重翻成功后覆盖成合格译文
    store.tm_put(conn, h, "fake", "<p>x</p>", "<p>好</p>", "v")
    assert store.tm_get(conn, h, "fake", "v") == "<p>好</p>"


def test_tm_put_rejects_unknown_quality(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        store.tm_put(conn, "h", "fake", "<p>x</p>", "<p>y</p>", quality="maybe")


def test_record_failure_with_http_status(conn: sqlite3.Connection) -> None:
    store.set_status(conn, URL, Status.FETCHED, http_status=200)
    store.record_failure(conn, URL, "HTTP 404", http_status=404)
    row = store.get_article(conn, URL)
    assert row is not None
    assert (row["status"], row["http_status"], row["retries"]) == ("failed", 404, 1)


def test_record_failure_none_clears_http_status(conn: sqlite3.Connection) -> None:
    store.set_status(conn, URL, Status.FETCHED, http_status=200)
    store.record_failure(conn, URL, "请求失败（超时）", http_status=None)
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["http_status"] is None


def test_record_failure_without_http_status_keeps_it(conn: sqlite3.Connection) -> None:
    store.set_status(conn, URL, Status.FETCHED, http_status=200)
    store.record_failure(conn, URL, "extract: ValueError")
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["http_status"] == 200


def test_new_columns_are_updatable(conn: sqlite3.Connection) -> None:
    store.set_status(
        conn,
        URL,
        Status.EXTRACTED,
        last_modified="Wed, 11 Nov 2026 00:00:00 GMT",
        qc_issues='["正文太短"]',
    )
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["last_modified"] == "Wed, 11 Nov 2026 00:00:00 GMT"
    assert row["qc_issues"] == '["正文太短"]'


def test_changed_lastmod_clears_last_modified(conn: sqlite3.Connection) -> None:
    store.set_status(conn, URL, Status.FETCHED, last_modified="Wed, 11 Nov 2026 00:00:00 GMT")
    store.upsert_discovered(conn, DiscoveredUrl(URL, "example", "a", "2026-02-01"))
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["last_modified"] is None


def test_needs_review_status_exists() -> None:
    assert Status("needs_review") is Status.NEEDS_REVIEW
