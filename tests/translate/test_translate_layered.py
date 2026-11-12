"""分层翻译：translate --titles 只翻标题不改状态；translate --url-file 按清单翻全文。"""

from pathlib import Path

import pytest
import yaml

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, translate_article

SITE = "man"
GLOSSARY = Glossary("1", {})


def _db(tmp_path: Path, slugs: list[str]) -> tuple[Path, list[str]]:
    db = tmp_path / "s.db"
    conn = store.connect(db)
    urls = []
    for slug in slugs:
        url = f"https://www.man.com/insights/{slug}"
        urls.append(url)
        store.upsert_discovered(conn, DiscoveredUrl(url, SITE, slug))
        store.replace_segments(conn, url, [Segment(0, f"<p>Body {slug}.</p>")])
        store.set_status(conn, url, Status.EXTRACTED, title=f"Title {slug}")
    conn.close()
    return db, urls


@pytest.mark.usefixtures("man_without_sections")
def test_titles_on_extracted_keeps_status(tmp_path: Path) -> None:
    db, (url, *_rest) = _db(tmp_path, ["x"])
    t = FakeTranslator()
    argv = ["translate", "--site", SITE, "--db", str(db), "--titles"]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == 0
    conn = store.connect(db)
    row = store.get_article(conn, url)
    conn.close()
    assert row is not None
    assert row["title_zh"] == "【译】Title x"
    assert row["status"] == Status.EXTRACTED  # 标题有了 ≠ 全文翻了
    assert t.calls == [["<p>Title x</p>"]]  # 只翻标题，正文一段没送


@pytest.mark.usefixtures("man_without_sections")
def test_url_file_translates_listed_articles(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, urls = _db(tmp_path, ["a", "b", "c"])
    url_file = tmp_path / "urls.txt"
    url_file.write_text(urls[0] + "\n" + urls[2] + "\n", encoding="utf-8")
    t = FakeTranslator()
    argv = ["translate", "--site", SITE, "--db", str(db), "--url-file", str(url_file)]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == 0
    assert "预计约" in capsys.readouterr().out  # 开始前打印预计字符数
    conn = store.connect(db)
    assert store.get_article(conn, urls[0])["status"] == Status.TRANSLATED  # type: ignore[index]
    assert store.get_article(conn, urls[1])["status"] == Status.EXTRACTED  # type: ignore[index]
    assert store.get_article(conn, urls[2])["status"] == Status.TRANSLATED  # type: ignore[index]
    conn.close()


@pytest.mark.usefixtures("man_without_sections")
def test_bare_translate_does_not_sweep_title_only_backlog(tmp_path: Path) -> None:
    """H2：裸 translate / --retry-rejected 不把 published+translator 为空扫进去。"""
    db, urls = _db(tmp_path, ["a", "b"])
    conn = store.connect(db)
    for u in urls:
        store.set_status(conn, u, Status.PUBLISHED, title_zh="【译】", translator=None)
    conn.close()
    t = FakeTranslator()
    argv = ["translate", "--site", SITE, "--db", str(db), "--retry-rejected"]
    assert cli.main(argv, deps=cli.Deps(translator=t)) == 0
    assert t.calls == []  # 一篇都没翻


@pytest.mark.usefixtures("man_without_sections")
def test_full_translate_run_picks_up_title_only_backlog(tmp_path: Path) -> None:
    """H2：run --full-translate 才会把 published+translator 为空选进来补翻全文。"""
    db, urls = _db(tmp_path, ["a", "b"])
    conn = store.connect(db)
    for u in urls:
        store.set_status(conn, u, Status.PUBLISHED, title_zh="【译】", translator=None)
    conn.close()
    argv = [
        "run",
        "--site",
        SITE,
        "--db",
        str(db),
        "--out",
        str(tmp_path / "o"),
        "--no-images",
        "--full-translate",
        "--skip",
        "discover",
        "--skip",
        "fetch",
        "--skip",
        "extract",
        "--skip",
        "summarize",
    ]
    assert cli.main(argv, deps=cli.Deps(translator=FakeTranslator())) == 0
    conn = store.connect(db)
    for u in urls:
        assert store.get_article(conn, u)["translator"] == "fake"  # type: ignore[index]
    conn.close()


@pytest.mark.usefixtures("man_without_sections")
def test_url_translate_all_tm_hit_still_writes_translator(tmp_path: Path) -> None:
    """H3：全部命中翻译记忆也要写 translator，否则永远被重新选中。"""
    db, (url,) = _db(tmp_path, ["x"])
    conn = store.connect(db)
    translate_article(conn, url, FakeTranslator(), GLOSSARY)  # 填热翻译记忆 + translator
    store.set_status(conn, url, Status.PUBLISHED, translator=None)  # 模拟「只翻标题发布」
    conn.execute("UPDATE articles SET translator=NULL WHERE url=?", (url,))
    conn.commit()
    conn.close()
    assert (
        cli.main(
            ["translate", "--site", SITE, "--db", str(db), "--url", url],
            deps=cli.Deps(translator=FakeTranslator()),
        )
        == 0
    )
    conn = store.connect(db)
    row = store.get_article(conn, url)
    conn.close()
    assert row is not None and row["translator"] == "fake"  # 全部命中记忆也落 translator
    assert row["status"] == Status.TRANSLATED


@pytest.mark.usefixtures("man_without_sections")
def test_url_file_bom_and_missing_and_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M3/ds-3：BOM 清单能读；查不到的逐条列出、全查不到退出非 0；状态不对跳过不打 failed。"""
    db, urls = _db(tmp_path, ["a", "b"])
    # b 没有段落、状态 discovered：被 --url 点名应跳过、不打成 failed
    conn = store.connect(db)
    store.replace_segments(conn, urls[1], [])
    store.set_status(conn, urls[1], Status.DISCOVERED)
    conn.close()
    f = tmp_path / "bom.txt"
    f.write_text(urls[0] + "\n", encoding="utf-8-sig")  # 带 BOM
    assert (
        cli.main(
            ["translate", "--site", SITE, "--db", str(db), "--url-file", str(f)],
            deps=cli.Deps(translator=FakeTranslator()),
        )
        == 0
    )
    conn = store.connect(db)
    assert store.get_article(conn, urls[0])["status"] == Status.TRANSLATED  # type: ignore[index]
    conn.close()

    # 只有查不到的网址 → 非 0
    f.write_text("https://www.man.com/insights/nope\n", encoding="utf-8")
    assert (
        cli.main(
            ["translate", "--site", SITE, "--db", str(db), "--url-file", str(f)],
            deps=cli.Deps(translator=FakeTranslator()),
        )
        == cli.EXIT_ATTENTION
    )
    assert "清单里找不到" in capsys.readouterr().out

    # 状态不对（discovered、无段落）→ 跳过、不 failed
    rc = cli.main(
        ["translate", "--site", SITE, "--db", str(db), "--url", urls[1]],
        deps=cli.Deps(translator=FakeTranslator()),
    )
    assert rc == cli.EXIT_ATTENTION
    assert "跳过" in capsys.readouterr().out
    conn = store.connect(db)
    assert store.get_article(conn, urls[1])["status"] == Status.DISCOVERED  # type: ignore[index]
    conn.close()


@pytest.mark.usefixtures("man_without_sections")
def test_url_other_site_is_skipped_not_failed(tmp_path: Path) -> None:
    """ds-2：--url 点名的网址属于别的站点时，本站点循环跳过、不打 failed。"""
    db = tmp_path / "s.db"
    conn = store.connect(db)
    other = "https://www.other.test/insights/x"
    store.upsert_discovered(conn, DiscoveredUrl(other, "other", "x"))
    store.replace_segments(conn, other, [Segment(0, "<p>Body.</p>")])
    store.set_status(conn, other, Status.EXTRACTED, title="T")
    conn.close()
    assert (
        cli.main(
            ["translate", "--site", SITE, "--db", str(db), "--url", other],
            deps=cli.Deps(translator=FakeTranslator()),
        )
        == 0
    )
    conn = store.connect(db)
    assert store.get_article(conn, other)["status"] == Status.EXTRACTED  # type: ignore[index]
    conn.close()


def _multi_site_config(tmp_path: Path, names: list[str]) -> Path:
    cfg = tmp_path / "config"
    (cfg / "sites").mkdir(parents=True)
    for name in names:
        (cfg / "sites" / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {"compliance": {"decision": "allowed"}, "base_url": f"https://{name}.test"},
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    (cfg / "glossary.yaml").write_text(
        yaml.safe_dump({"version": 1, "terms": {}}, allow_unicode=True), encoding="utf-8"
    )
    return cfg


def test_url_file_multisite_translates_each_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ds-2：多站点 --url-file 里每个网址只在它所属站点翻一次（不会每站点重复处理）。"""
    cfg = _multi_site_config(tmp_path, ["a", "b"])
    db = tmp_path / "s.db"
    conn = store.connect(db)
    urls = []
    for site, slug in [("a", "x"), ("b", "y")]:
        url = f"https://{site}.test/insights/{slug}"
        urls.append(url)
        store.upsert_discovered(conn, DiscoveredUrl(url, site, slug))
        store.replace_segments(conn, url, [Segment(0, f"<p>Body {slug}.</p>")])
        store.set_status(conn, url, Status.EXTRACTED, title=f"Title {slug}")
    conn.close()
    f = tmp_path / "urls.txt"
    f.write_text("\n".join(urls) + "\n", encoding="utf-8")
    assert (
        cli.main(
            ["translate", "--db", str(db), "--config-dir", str(cfg), "--url-file", str(f)],
            deps=cli.Deps(translator=FakeTranslator()),
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.count("待翻译 1 篇") == 2  # 每个站点各 1 篇，而不是每个站点都处理 2 篇
    assert "待翻译 2 篇" not in out


@pytest.mark.usefixtures("man_without_sections")
def test_url_file_preserves_manifest_order(tmp_path: Path) -> None:
    """M4：按清单顺序翻（不是字典序）。额度只够第一篇 z 时，翻 z 而不是字母序的 x。"""
    db, urls = _db(tmp_path, ["z", "y", "x"])
    f = tmp_path / "urls.txt"
    f.write_text("\n".join(urls) + "\n", encoding="utf-8")  # 顺序 z,y,x
    quota = len("<p>Body z.</p>") + len("<p>Title z</p>")
    assert (
        cli.main(
            ["translate", "--site", SITE, "--db", str(db), "--url-file", str(f)],
            deps=cli.Deps(translator=FakeTranslator(quota_chars=quota)),
        )
        == cli.EXIT_QUOTA
    )
    conn = store.connect(db)
    assert store.get_article(conn, urls[0])["status"] == Status.TRANSLATED  # z 翻了  # type: ignore[index]
    assert store.get_article(conn, urls[2])["status"] == Status.EXTRACTED  # x（字母序第一）没翻  # type: ignore[index]
    conn.close()


class _QuotaAware(FakeTranslator):
    """带 usage() 的假后端：测额度显示和超额度警告。"""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__()
        self._used = used
        self._limit = limit

    def usage(self) -> tuple[int, int]:
        return self._used, self._limit


@pytest.mark.usefixtures("man_without_sections")
def test_titles_stage_prints_quota(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """ds-5：titles 阶段补完标题后打印额度，控制台额度栏才会更新。"""
    db, (url,) = _db(tmp_path, ["x"])
    cli.main(
        ["translate", "--site", SITE, "--db", str(db), "--titles"],
        deps=cli.Deps(translator=_QuotaAware(100, 1_000_000)),
    )
    assert "[titles] 翻译后本月额度" in capsys.readouterr().out


@pytest.mark.usefixtures("man_without_sections")
def test_url_file_warns_when_estimate_exceeds_quota(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """M4：预计字符超过剩余额度时，开始前打印醒目警告。"""
    db, urls = _db(tmp_path, ["a", "b"])
    f = tmp_path / "urls.txt"
    f.write_text("\n".join(urls) + "\n", encoding="utf-8")
    # 剩余额度为 0：任何预计字符都会触发警告
    cli.main(
        ["translate", "--site", SITE, "--db", str(db), "--url-file", str(f)],
        deps=cli.Deps(translator=_QuotaAware(1_000_000, 1_000_000)),
    )
    assert "超过本月剩余额度" in capsys.readouterr().err


@pytest.mark.usefixtures("man_without_sections")
def test_url_with_recheck_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """ds-6：--url / --url-file 与 --recheck 一起用直接报错。"""
    db, (url,) = _db(tmp_path, ["x"])
    rc = cli.main(
        ["translate", "--site", SITE, "--db", str(db), "--url", url, "--recheck"],
        deps=cli.Deps(translator=FakeTranslator()),
    )
    assert rc == cli.EXIT_ERROR
    assert "不能和 --recheck" in capsys.readouterr().err


@pytest.mark.usefixtures("man_without_sections")
def test_run_without_llm_key_skips_summarize_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """H1：默认 run 没配 LLM_API_KEY 时跳过摘要（提示清楚），export 照常跑，退出码按部分完成。"""
    db, (url,) = _db(tmp_path, ["x"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    argv = [
        "run",
        "--site",
        SITE,
        "--db",
        str(db),
        "--out",
        str(tmp_path / "o"),
        "--no-images",
        "--skip",
        "discover",
        "--skip",
        "fetch",
        "--skip",
        "extract",
    ]
    assert cli.main(argv, deps=cli.Deps(translator=FakeTranslator())) == cli.EXIT_ATTENTION
    out = capsys.readouterr().out
    assert "没配 LLM_API_KEY，跳过中文摘要" in out
    conn = store.connect(db)
    assert store.get_article(conn, url)["status"] == Status.PUBLISHED  # export 照常跑  # type: ignore[index]
    conn.close()
    assert list((tmp_path / "o").rglob("*.md"))


@pytest.mark.usefixtures("man_without_sections")
def test_standalone_summarize_without_key_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """H1：独立 qbkb summarize 没配 key 保持报错（退出码 1）。"""
    db, _url = _db(tmp_path, ["x"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert cli.main(["summarize", "--site", SITE, "--db", str(db)]) == cli.EXIT_ERROR
    assert "LLM_API_KEY" in capsys.readouterr().err
