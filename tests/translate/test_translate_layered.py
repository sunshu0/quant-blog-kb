"""分层翻译：translate --titles 只翻标题不改状态；translate --url-file 按清单翻全文。"""

from pathlib import Path

import pytest

from qbkb import cli, store
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator

SITE = "man"


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
