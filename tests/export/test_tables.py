"""表格只使用构造内容，不调用外部服务。"""

import csv
import json
import sqlite3
from pathlib import Path

from qbkb import cli, store
from qbkb.config import SiteConfig
from qbkb.export.tables import COLUMNS, write_tables
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, translate_article

SITE = SiteConfig("ex", "allowed", {"base_url": "https://ex.test", "firm": "构造公司"})
GLOSSARY = Glossary("1", {})
URL = "https://ex.test/example"


def add(conn: sqlite3.Connection, url: str = URL) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", url.rsplit("/", 1)[-1]))
    store.replace_segments(
        conn,
        url,
        [
            Segment(0, '<p>A made-up "quoted" example, 数据.</p>'),
            Segment(1, "<h2>Second block</h2>"),
        ],
    )
    store.set_status(
        conn,
        url,
        Status.EXTRACTED,
        title="Constructed title",
        published="2026-01-02",
        tags='["Test", "构造"]',
        qc_issues='["构造提示"]',
    )


def test_round_trip_multiline_unicode_and_complete_translation(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    add(conn)
    translate_article(conn, URL, FakeTranslator(), GLOSSARY)
    stats = write_tables(conn, SITE, GLOSSARY, tmp_path / "out")
    assert stats.rows == 1
    assert stats.csv.read_bytes().startswith(bytes([239, 187, 191]))
    with stats.csv.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    data = [json.loads(line) for line in stats.jsonl.read_text(encoding="utf-8").splitlines()]
    assert list(data[0]) == list(COLUMNS)
    assert len(rows) == len(data) == 1
    assert rows[0]["英文正文"] == data[0]["英文正文"]
    assert "\n\n" in data[0]["英文正文"] and "<p>" not in data[0]["英文正文"]
    assert data[0]["中文正文"] and data[0]["标签"] == ["Test", "构造"]
    assert data[0]["公司名"] == "构造公司" and data[0]["中文摘要"] == data[0]["抓取时间"] == ""
    before = stats.csv.stat().st_mtime_ns
    write_tables(conn, SITE, GLOSSARY, tmp_path / "out")
    assert stats.csv.stat().st_mtime_ns == before
    conn.close()


def test_incomplete_translation_never_emits_partial_body(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    add(conn)
    translate_article(conn, URL, FakeTranslator(), GLOSSARY)
    conn.execute(
        "DELETE FROM translation_memory WHERE src_hash IN "
        "(SELECT src_hash FROM segments WHERE article_url = ? AND idx = 1)",
        (URL,),
    )
    conn.commit()
    stats = write_tables(conn, SITE, GLOSSARY, tmp_path / "out")
    row = json.loads(stats.jsonl.read_text(encoding="utf-8"))
    assert row["中文正文"] == "" and row["翻译状态"] == "incomplete"
    conn.close()


def test_snapshot_keeps_older_rows_and_other_sites(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    add(conn)
    out = tmp_path / "out"
    other = out / "tables" / "other.jsonl"
    other.parent.mkdir(parents=True)
    other.write_text("other content", encoding="utf-8")
    write_tables(conn, SITE, GLOSSARY, out)
    add(conn, URL + "-new")
    stats = write_tables(conn, SITE, GLOSSARY, out)
    assert stats.rows == 2 and other.read_text(encoding="utf-8") == "other content"
    store.set_status(conn, URL, Status.UNSELECTED)
    store.set_status(conn, URL + "-new", Status.UNSELECTED)
    stats = write_tables(conn, SITE, GLOSSARY, out)
    assert stats.rows == 0 and stats.jsonl.read_text(encoding="utf-8") == ""
    assert len(stats.csv.read_text(encoding="utf-8-sig").splitlines()) == 1
    conn.close()


def test_table_cli_does_not_need_keys(tmp_path: Path) -> None:
    cfg = tmp_path / "config"
    (cfg / "sites").mkdir(parents=True)
    (cfg / "sites" / "ex.yaml").write_text(
        "base_url: https://ex.test\nfirm: Example\ncompliance: {decision: allowed}\n",
        encoding="utf-8",
    )
    (cfg / "glossary.yaml").write_text("version: '1'\nterms: {}\n", encoding="utf-8")
    db = tmp_path / "state.db"
    conn = store.connect(db)
    add(conn)
    conn.close()
    assert (
        cli.main(
            [
                "table",
                "--site",
                "ex",
                "--db",
                str(db),
                "--config-dir",
                str(cfg),
                "--out",
                str(tmp_path / "out"),
            ]
        )
        == 0
    )
    row = json.loads((tmp_path / "out" / "tables" / "ex.jsonl").read_text(encoding="utf-8"))
    assert row["中文正文"] == "" and row["翻译状态"] == "extracted"


def test_unmigrated_prefix_article_not_exported(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    add(conn)
    site = SiteConfig(
        "ex",
        "allowed",
        {
            **SITE.raw,
            "sections": {
                "list": [{"id": "all", "name": "All", "url_prefix": "/", "collect": True}]
            },
        },
    )
    stats = write_tables(conn, site, GLOSSARY, tmp_path / "out")
    assert stats.rows == 0
    store.set_fields(conn, URL, section_key="url:", section="all")
    assert write_tables(conn, site, GLOSSARY, tmp_path / "out").rows == 1
    conn.close()


def test_published_history_survives_null_key_and_cancelled_section(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    add(conn)
    translate_article(conn, URL, FakeTranslator(), GLOSSARY)
    store.set_status(conn, URL, Status.PUBLISHED)
    site = SiteConfig(
        "ex",
        "allowed",
        {
            **SITE.raw,
            "sections": {
                "list": [{"id": "all", "name": "All", "url_prefix": "/", "collect": False}]
            },
        },
    )
    out = tmp_path / "out"
    assert write_tables(conn, site, GLOSSARY, out).rows == 1
    store.set_fields(conn, URL, section_key="url:", section="all")
    stats = write_tables(conn, site, GLOSSARY, out)
    row = json.loads(stats.jsonl.read_text(encoding="utf-8"))
    assert stats.rows == 1 and row["中文正文"]
    conn.close()


def test_rejected_translation_does_not_leak_into_table(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    add(conn)
    translate_article(conn, URL, FakeTranslator(), GLOSSARY)
    conn.execute("UPDATE translation_memory SET quality = 'rejected'")
    conn.commit()
    store.set_status(conn, URL, Status.NEEDS_REVIEW)
    stats = write_tables(conn, SITE, GLOSSARY, tmp_path / "out")
    row = json.loads(stats.jsonl.read_text(encoding="utf-8"))
    assert row["中文正文"] == "" and row["翻译状态"] == "incomplete"
    conn.close()


def test_table_uses_fetch_time_without_falling_back_to_update_time(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    out = tmp_path / "out"
    stats = write_tables(conn, SITE, GLOSSARY, out)
    assert json.loads(stats.jsonl.read_text(encoding="utf-8"))["抓取时间"] == ""
    store.set_fields(conn, URL, fetched_at="2026-01-01T00:00:00+00:00")
    write_tables(conn, SITE, GLOSSARY, out)
    assert (
        json.loads(stats.jsonl.read_text(encoding="utf-8"))["抓取时间"]
        == "2026-01-01T00:00:00+00:00"
    )
    conn.close()
