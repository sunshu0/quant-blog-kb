"""Phase 2 的状态库改动：body_segments 列（v2 迁移）、count_by_status、list_rows。"""

import sqlite3
from pathlib import Path

from qbkb import store
from qbkb.models import DiscoveredUrl, Status
from qbkb.store import db as store_db


def test_v1_database_gets_body_segments(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    store.connect(path).close()
    raw = sqlite3.connect(path)
    # 模拟 v1 的库：去掉新列、版本号退回 1
    raw.execute("ALTER TABLE articles DROP COLUMN body_segments")
    raw.execute("PRAGMA user_version = 1")
    raw.commit()
    raw.close()

    conn = store.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(articles)")}
    assert "body_segments" in cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store_db.SCHEMA_VERSION == 2
    store.upsert_discovered(conn, DiscoveredUrl("https://e.test/a", "e", "a"))
    store.set_status(conn, "https://e.test/a", Status.EXTRACTED, body_segments=3)
    row = store.get_article(conn, "https://e.test/a")
    assert row is not None and row["body_segments"] == 3
    conn.close()


def test_count_by_status_and_list_rows(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "s.db")
    for site, slug in (("a", "1"), ("a", "2"), ("b", "3")):
        store.upsert_discovered(conn, DiscoveredUrl(f"https://e.test/{slug}", site, slug))
    store.set_status(conn, "https://e.test/2", Status.FAILED, error="x")
    assert store.count_by_status(conn) == {Status.DISCOVERED: 2, Status.FAILED: 1}
    assert store.count_by_status(conn, "b") == {Status.DISCOVERED: 1}
    rows = store.list_rows(conn, [Status.FAILED, Status.DISCOVERED], "a")
    assert [r["slug"] for r in rows] == ["1", "2"]
    assert store.list_rows(conn, []) == []
    conn.close()
