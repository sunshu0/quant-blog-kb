"""PR #7 审查修复：需要人工处理时的退出码 5、抓取间隔下限、translate --titles。"""

from pathlib import Path

import pytest

from qbkb import cli, config, store
from qbkb.fetch.settings import DOWNLOAD_DELAY_SECONDS
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, translate_article

URL = "https://www.man.com/insights/x"


def test_help_lists_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    out = capsys.readouterr().out
    assert "退出码" in out and "5  跑完了，但有文章需要人工处理" in out


def test_export_failure_exits_attention(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "s.db"
    conn = store.connect(db)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "x"))
    store.set_status(conn, URL, Status.TRANSLATED)  # 没有段落、没有 translator：导出失败
    conn.close()
    argv = ["export", "--site", "man", "--db", str(db), "--out", str(tmp_path / "o")]
    assert cli.main([*argv, "--no-images"]) == cli.EXIT_ATTENTION
    assert "需要人工处理" in capsys.readouterr().out
    # 没有待处理的文章：正常退出
    assert cli.main([*argv, "--no-images"]) == cli.EXIT_OK


def test_requeue_exits_attention(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    conn = store.connect(db)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "x"))
    store.replace_segments(conn, URL, [Segment(0, "<p>A momentum note.</p>")])
    store.set_status(conn, URL, Status.FILTERED, title="T")
    # 用和 config/glossary.yaml 不同的术语表翻：导出时查不到译文，退回 filtered
    translate_article(conn, URL, FakeTranslator(), Glossary("0", {"momentum": "势头"}))
    conn.close()
    argv = ["run", "--site", "man", "--db", str(db), "--out", str(tmp_path / "o"), "--no-images"]
    skips = [
        x for s in ("discover", "fetch", "extract", "filter", "translate") for x in ("--skip", s)
    ]
    assert cli.main([*argv, *skips]) == cli.EXIT_ATTENTION
    conn = store.connect(db)
    row = store.get_article(conn, URL)
    conn.close()
    assert row is not None and row["status"] == Status.FILTERED


def test_quota_wins_over_attention(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    conn = store.connect(db)
    for slug in ("a", "b"):
        url = f"https://www.man.com/insights/{slug}"
        store.upsert_discovered(conn, DiscoveredUrl(url, "man", slug))
        store.replace_segments(conn, url, [Segment(0, f"<p>Text {slug}.</p>")])
        store.set_status(conn, url, Status.FILTERED, title=None)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "x"))
    store.set_status(conn, URL, Status.TRANSLATED)  # 导出会失败
    conn.close()
    deps = cli.Deps(translator=FakeTranslator(quota_chars=len("<p>Text a.</p>")))
    argv = ["run", "--site", "man", "--db", str(db), "--out", str(tmp_path / "o"), "--no-images"]
    skips = [x for s in ("discover", "fetch", "extract", "filter") for x in ("--skip", s)]
    assert cli.main([*argv, *skips], deps=deps) == cli.EXIT_QUOTA


def test_fetch_delay_floor_for_real_sites(capsys: pytest.CaptureFixture[str]) -> None:
    man = config.load_site("man")
    assert cli.fetch_delay_overrides(0, man) == {}  # 真实站点：忽略，用默认间隔
    assert "不能低于" in capsys.readouterr().out
    assert cli.fetch_delay_overrides(None, man) == {}
    slower = cli.fetch_delay_overrides(DOWNLOAD_DELAY_SECONDS + 3, man)
    assert slower["DOWNLOAD_DELAY"] == DOWNLOAD_DELAY_SECONDS + 3
    for base in ("http://127.0.0.1:8131", "http://localhost", "http://site.test"):
        local = config.SiteConfig("l", "allowed", {"base_url": base})
        assert cli.fetch_delay_overrides(0, local) == {
            "DOWNLOAD_DELAY": 0,
            "AUTOTHROTTLE_START_DELAY": 0,
            "AUTOTHROTTLE_ENABLED": False,
        }


@pytest.mark.parametrize(
    ("host", "local"),
    [
        ("localhost", True),
        ("127.0.0.1", True),
        ("a.localhost", True),
        ("site.test", True),
        ("www.man.com", False),
        ("test.com", False),
        ("evil.test.com", False),
        (None, False),
    ],
)
def test_is_local_host(host: str | None, local: bool) -> None:
    assert config.is_local_host(host) is local


def test_translate_titles_option(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "s.db"
    conn = store.connect(db)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "x"))
    store.replace_segments(conn, URL, [Segment(0, "<p>Body.</p>")])
    store.set_status(conn, URL, Status.PUBLISHED, title="Old article", translator="fake")
    conn.close()
    t = FakeTranslator()
    argv = ["translate", "--site", "man", "--db", str(db), "--titles"]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == cli.EXIT_OK
    assert t.calls == [["<p>Old article</p>"]]
    assert "补翻标题 1 / 1 篇" in capsys.readouterr().out
    conn = store.connect(db)
    row = store.get_article(conn, URL)
    conn.close()
    assert row is not None and row["title_zh"] == "【译】Old article"
    assert row["status"] == Status.TRANSLATED
    assert cli.main(argv, deps=cli.Deps(translator=t)) == cli.EXIT_OK
    assert "没有缺中文标题的文章" in capsys.readouterr().out
