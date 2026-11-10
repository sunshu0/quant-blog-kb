"""和状态库的衔接：fetched → extracted / failed。全部离线，原始 HTML 用黄金样本。"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from qbkb import config, store
from qbkb.extract import QC_PREFIX, extract, load_rules, run
from qbkb.models import DiscoveredUrl, Status

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "man"
GOOD_URL = "https://www.man.com/insights/fixture-valuation-spreads"
SHORT_URL = "https://www.man.com/insights/fixture-carry-video"
MISSING_URL = "https://www.man.com/insights/fixture-missing-raw"
EMPTY_URL = "https://www.man.com/insights/fixture-empty"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def _fetched(conn: sqlite3.Connection, url: str, raw_path: str | None) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="man", slug=url.rsplit("/", 1)[-1]))
    store.set_status(conn, url, Status.FETCHED, raw_path=raw_path, http_status=200)


def _setup(conn: sqlite3.Connection, tmp_path: Path) -> None:
    raw = tmp_path / "raw" / "man"
    raw.mkdir(parents=True)
    shutil.copy(GOLDEN / "article-figures.html", raw / "a.html")
    shutil.copy(GOLDEN / "video-short.html", raw / "b.html")
    (raw / "empty.html").write_text("<html><body></body></html>", encoding="utf-8")
    _fetched(conn, GOOD_URL, "raw/man/a.html")  # 相对路径：相对 root
    _fetched(conn, SHORT_URL, str(raw / "b.html"))  # 绝对路径
    _fetched(conn, MISSING_URL, "raw/man/nope.html")
    _fetched(conn, EMPTY_URL, "raw/man/empty.html")


def test_run_extracts_and_records(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    site = config.load_site("man")
    stats = run(conn, site, root=tmp_path)

    assert stats.extracted == 2
    assert stats.failed == 2
    assert set(stats.flagged) == {SHORT_URL}

    row = store.get_article(conn, GOOD_URL)
    assert row is not None
    assert row["status"] == Status.EXTRACTED
    expected = extract((GOLDEN / "article-figures.html").read_bytes(), GOOD_URL, load_rules(site))
    assert row["body_hash"] == expected.body_hash
    assert row["title"] == "Valuation Spreads in a Toy Universe"
    assert row["author"] == "Ada Fixture, Bo Sample"
    assert row["published"] == "2024-05-07"
    assert json.loads(row["tags"]) == ["ARTICLE"]
    assert row["error"] is None
    assert store.get_segments(conn, GOOD_URL) == list(expected.segments)

    short = store.get_article(conn, SHORT_URL)
    assert short is not None
    assert short["status"] == Status.EXTRACTED
    assert short["error"].startswith(QC_PREFIX)
    assert "正文太短" in short["error"]

    for url, reason in [(MISSING_URL, "FileNotFoundError"), (EMPTY_URL, "ExtractError")]:
        bad = store.get_article(conn, url)
        assert bad is not None
        assert bad["status"] == Status.FAILED
        assert bad["retries"] == 1
        assert reason in bad["error"]


def test_run_is_idempotent(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _setup(conn, tmp_path)
    site = config.load_site("man")
    run(conn, site, root=tmp_path)
    first = store.get_segment_hashes(conn, GOOD_URL)
    # 再跑一次：已经 extracted 的不再处理；手动退回 fetched 后重跑，结果一样
    assert run(conn, site, root=tmp_path).extracted == 0
    store.set_status(conn, GOOD_URL, Status.FETCHED)
    assert run(conn, site, root=tmp_path).extracted == 1
    assert store.get_segment_hashes(conn, GOOD_URL) == first


def test_run_only_touches_own_site(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url="https://o.test/x", site="other", slug="x"))
    store.set_status(conn, "https://o.test/x", Status.FETCHED, raw_path="nope.html")
    stats = run(conn, config.load_site("man"), root=tmp_path)
    assert (stats.extracted, stats.failed) == (0, 0)
    row = store.get_article(conn, "https://o.test/x")
    assert row is not None
    assert row["status"] == Status.FETCHED
