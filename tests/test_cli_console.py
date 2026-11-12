"""CLI 为控制台新增的参数：translate --url（「重翻」限定一篇）、--translator demo、
console 子命令。"""

from pathlib import Path

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator

A, B = "https://example.test/a", "https://example.test/b"


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = store.connect(db)
    for url in (A, B):
        store.upsert_discovered(conn, DiscoveredUrl(url, "man", url[-1]))
        store.replace_segments(conn, url, [Segment(0, f"<p>Words for {url[-1]}.</p>")])
        store.set_status(conn, url, Status.NEEDS_REVIEW, error="结构校验未通过的段落 0")
    conn.close()
    return db


def test_translate_url_limits_to_one_article(tmp_path: Path) -> None:
    db = _db(tmp_path)
    t = FakeTranslator()
    argv = ["translate", "--site", "man", "--db", str(db), "--retry-rejected", "--url", A]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == 0
    conn = store.connect(db)
    try:
        assert store.get_article(conn, A)["status"] == Status.TRANSLATED  # type: ignore[index]
        assert store.get_article(conn, B)["status"] == Status.NEEDS_REVIEW  # type: ignore[index]
    finally:
        conn.close()
    assert t.calls == [["<p>Words for a.</p>"]]  # 只翻了 A


def test_parser_has_console_and_demo_translator() -> None:
    p = cli.build_parser()
    args = p.parse_args(["console", "--port", "9999", "--no-browser"])
    assert args.command == "console" and args.port == 9999 and args.no_browser
    args = p.parse_args(["run", "--translator", "demo"])
    assert args.translator == "demo" and args.url == []


def test_make_translator_demo(monkeypatch: object) -> None:
    import argparse

    from qbkb.console.demo import DemoTranslator

    t = cli._make_translator(  # pyright: ignore[reportPrivateUsage]
        argparse.Namespace(translator="demo", fake_quota=None), cli.Deps()
    )
    assert isinstance(t, DemoTranslator)
