import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, Segment, Status

URL = "https://example.test/insights/a"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def _discover(conn: sqlite3.Connection, lastmod: str | None = "2026-01-01") -> bool:
    return store.upsert_discovered(
        conn, DiscoveredUrl(url=URL, site="example", slug="a", lastmod=lastmod)
    )


def test_connect_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store.connect(path).close()
    store.connect(path).close()


def test_new_url_needs_processing(conn: sqlite3.Connection) -> None:
    assert _discover(conn) is True
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.DISCOVERED


def test_same_lastmod_is_skipped(conn: sqlite3.Connection) -> None:
    _discover(conn)
    store.set_status(conn, URL, Status.TRANSLATED)
    assert _discover(conn) is False
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.TRANSLATED


def test_changed_lastmod_resets_to_discovered(conn: sqlite3.Connection) -> None:
    _discover(conn)
    store.set_status(conn, URL, Status.TRANSLATED)
    assert _discover(conn, lastmod="2026-02-01") is True
    row = store.get_article(conn, URL)
    assert row is not None
    assert row["status"] == Status.DISCOVERED
    assert row["lastmod"] == "2026-02-01"


def test_missing_lastmod_does_not_reset(conn: sqlite3.Connection) -> None:
    _discover(conn)
    store.set_status(conn, URL, Status.TRANSLATED)
    assert _discover(conn, lastmod=None) is False


def test_set_status_updates_fields(conn: sqlite3.Connection) -> None:
    _discover(conn)
    store.set_status(conn, URL, Status.FETCHED, http_status=200, raw_path="raw/example/a.html")
    row = store.get_article(conn, URL)
    assert row is not None
    assert (row["status"], row["http_status"], row["raw_path"]) == (
        "fetched",
        200,
        "raw/example/a.html",
    )
    assert store.list_by_status(conn, Status.FETCHED) == [URL]
    assert store.list_by_status(conn, Status.FETCHED, site="other") == []


def test_set_status_rejects_unknown_column(conn: sqlite3.Connection) -> None:
    _discover(conn)
    with pytest.raises(ValueError):
        store.set_status(conn, URL, Status.FETCHED, raw_pth="typo")


def test_set_status_unknown_url(conn: sqlite3.Connection) -> None:
    with pytest.raises(KeyError):
        store.set_status(conn, URL, Status.FETCHED)


def test_record_failure_counts_retries(conn: sqlite3.Connection) -> None:
    _discover(conn)
    assert store.record_failure(conn, URL, "HTTP 429") == 1
    assert store.record_failure(conn, URL, "HTTP 429") == 2
    row = store.get_article(conn, URL)
    assert row is not None
    assert (row["status"], row["error"]) == ("failed", "HTTP 429")


def test_replace_segments(conn: sqlite3.Connection) -> None:
    _discover(conn)
    first = [Segment(0, "<p>a</p>"), Segment(1, "<p>b</p>")]
    store.replace_segments(conn, URL, first)
    store.replace_segments(conn, URL, first[:1])
    assert store.get_segment_hashes(conn, URL) == [first[0].src_hash]


def test_translation_memory_roundtrip(conn: sqlite3.Connection) -> None:
    h = Segment(0, "<p>Momentum works.</p>").src_hash
    assert store.tm_get(conn, h, "deepl") is None
    store.tm_put(conn, h, "deepl", "<p>Momentum works.</p>", "<p>动量有效。</p>", "1")
    assert store.tm_get(conn, h, "deepl") == "<p>动量有效。</p>"
    assert store.tm_get(conn, h, "azure") is None
    store.tm_put(conn, h, "deepl", "<p>Momentum works.</p>", "<p>动量策略有效。</p>", "2")
    assert store.tm_get(conn, h, "deepl") == "<p>动量策略有效。</p>"
