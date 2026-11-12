"""export 阶段：文件、frontmatter、对照版、图片、附录、缺译文退回、index、幂等。段落都是构造的。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from qbkb import export, store
from qbkb.config import SiteConfig
from qbkb.export import ExportStats, ImageFetcher, build_index, export_article, run
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, translate_article

SITE = SiteConfig("ex", "allowed", {"base_url": "https://ex.test", "firm": "Example Capital"})
URL = "https://ex.test/insights/carry"
IMG = "https://ex.test/files/fig1.png"
PARAS = [
    "<p>Trend following is a simple idea. Trend following again.</p>",
    f'<figure><img src="{IMG}" alt="Figure one"><figcaption>Figure 1</figcaption></figure>',
    "<h2>Results</h2>",
    "<p>Important information: this page is a made-up example.</p>",
]
GLOSSARY = Glossary("1", {"trend following": "趋势跟踪"})
CATS = ("量化方法论", "基础概念")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def add_translated(
    conn: sqlite3.Connection,
    url: str = URL,
    slug: str = "carry-1",
    published: str | None = "2024-03-05",
    category: str = "量化方法论",
) -> str:
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", slug))
    store.replace_segments(conn, url, [Segment(i, h) for i, h in enumerate(PARAS)])
    store.set_status(
        conn,
        url,
        Status.FILTERED,
        title="Carry & Trend",
        author="A. Writer",
        published=published,
        category=category,
        value_score=8.0,
        rubric_version="v1@fake",
        body_segments=3,
    )
    translate_article(conn, url, FakeTranslator(), GLOSSARY)
    return url


def png_transport(hits: list[str]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"PNG")

    return httpx.MockTransport(handler)


def split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    _, head, body = text.split("---\n", 2)
    data = yaml.safe_load(head)
    assert isinstance(data, dict)
    return data, body


def test_export_article_files_and_frontmatter(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    out = tmp_path / "output"
    hits: list[str] = []
    images = ImageFetcher(
        SITE, out / "ex" / "images", transport=png_transport(hits), sleep=lambda s: None
    )
    stats = run(conn, SITE, GLOSSARY, out, images=images, categories=CATS)
    assert stats.published == 1 and stats.images_downloaded == 1 and hits == [IMG]

    main = out / "ex" / "2024-03-05_carry-1.md"
    fm, body = split_frontmatter(main.read_text(encoding="utf-8"))
    assert list(fm) == [
        "title",
        "title_zh",
        "firm",
        "author",
        "published",
        "source_url",
        "section",
        "category",
        "value_score",
        "rubric_version",
        "translator",
        "translated_at",
    ]
    assert fm["title"] == "Carry & Trend" and fm["title_zh"] == "【译】Carry & Trend"
    assert fm["firm"] == "Example Capital" and fm["source_url"] == URL
    assert fm["value_score"] == 8.0 and fm["translator"] == "fake"
    assert fm["published"] == "2024-03-05"

    assert body.lstrip().startswith("# 【译】Carry & Trend\n\n> 来源：Example Capital")
    assert "原文版权归 Example Capital 所有" in body
    # 术语第一次出现附英文，第二次不附
    assert body.count("趋势跟踪（trend following）") == 1 and body.count("趋势跟踪") == 2
    # 图片改成相对路径
    name = export.image_stem(IMG) + ".png"
    assert f"](images/{name})" in body and IMG not in body
    assert (out / "ex" / "images" / name).read_bytes() == b"PNG"
    # 原文的重要信息（附录）保留译文，放在分隔线之后
    assert "---\n\n【译】Important information" in body
    assert "Trend following is" not in body  # 主文件是纯中文

    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.PUBLISHED
    assert row["md_path"] == main.resolve().as_posix()


def test_bilingual_version(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    out = tmp_path / "output"
    images = ImageFetcher(
        SITE, out / "ex" / "images", transport=png_transport([]), sleep=lambda s: None
    )
    run(conn, SITE, GLOSSARY, out, images=images)
    text = (out / "bilingual" / "ex" / "2024-03-05_carry-1.md").read_text(encoding="utf-8")
    en = "Trend following is a simple idea. Trend following again."
    assert f"{en}\n\n【译】趋势跟踪（trend following） is a simple idea." in text
    name = export.image_stem(IMG) + ".png"
    assert f"](../../ex/images/{name})" in text
    assert "](images/" not in text


def test_no_images_keeps_original_links(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    out = tmp_path / "output"
    run(conn, SITE, GLOSSARY, out, images=None)
    assert f"]({IMG})" in (out / "ex" / "2024-03-05_carry-1.md").read_text(encoding="utf-8")
    assert not (out / "ex" / "images").exists()


def test_image_failure_keeps_link_and_is_recorded(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    out = tmp_path / "output"
    transport = httpx.MockTransport(lambda req: httpx.Response(500))
    images = ImageFetcher(SITE, out / "ex" / "images", transport=transport, sleep=lambda s: None)
    stats = run(conn, SITE, GLOSSARY, out, images=images)
    assert stats.published == 1
    assert [f.image_url for f in stats.image_failures] == [IMG]
    assert f"]({IMG})" in (out / "ex" / "2024-03-05_carry-1.md").read_text(encoding="utf-8")
    log = (out / "ex" / "images" / "failed.tsv").read_text(encoding="utf-8")
    assert URL in log and IMG in log and "HTTP 500" in log


def test_missing_translation_requeues(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    newer = Glossary("2", {"trend following": "趋势追踪"})  # 术语改了：含这个术语的段落要重翻
    stats = run(conn, SITE, newer, tmp_path / "output")
    assert stats.requeued == [URL]
    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.EXTRACTED  # v2：退回 extracted
    assert "缺 1 段译文" in row["error"]


def test_idempotent_and_rebuild(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    out = tmp_path / "output"
    run(conn, SITE, GLOSSARY, out, categories=CATS)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files}
    second = run(conn, SITE, GLOSSARY, out, categories=CATS)
    assert second.published == 0  # 没有新的 translated
    rebuilt = run(conn, SITE, GLOSSARY, out, categories=CATS, rebuild=True)
    assert rebuilt.published == 1 and rebuilt.unchanged == 1
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files} == before


def test_published_date_change_removes_old_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    out = tmp_path / "output"
    run(conn, SITE, GLOSSARY, out)
    store.set_status(conn, URL, Status.TRANSLATED, published="2024-04-01")
    run(conn, SITE, GLOSSARY, out)
    names = sorted(p.name for p in (out / "ex").glob("*.md"))
    assert names == ["2024-04-01_carry-1.md"]
    assert sorted(p.name for p in (out / "bilingual" / "ex").glob("*.md")) == names


def test_export_failure_is_recorded(conn: sqlite3.Connection, tmp_path: Path) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(URL, "ex", "carry-1"))
    store.set_status(conn, URL, Status.TRANSLATED)  # 没有 translator、没有段落
    stats = run(conn, SITE, GLOSSARY, tmp_path / "output")
    assert len(stats.failed) == 1
    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.FAILED
    assert row["error"].startswith("export: ")


def test_index_groups_by_category(conn: sqlite3.Connection, tmp_path: Path) -> None:
    add_translated(conn)
    add_translated(conn, "https://ex.test/insights/b", "b-2", "2024-06-01", "基础概念")
    add_translated(conn, "https://ex.test/insights/c", "c-3", "2023-01-01")
    add_translated(conn, "https://ex.test/insights/d", "d-4", None, "未知类别")
    out = tmp_path / "output"
    run(conn, SITE, GLOSSARY, out, categories=CATS)
    text = (out / "index.md").read_text(encoding="utf-8")
    assert text.startswith("# 量化博客中文知识库\n\n共 4 篇")
    heads = [line for line in text.splitlines() if line.startswith("## ")]
    assert heads == ["## 量化方法论（2）", "## 基础概念（1）", "## 未知类别（1）"]
    entry = (
        "- [【译】Carry & Trend](ex/2024-03-05_carry-1.md) — 2024-03-05 · 8 分"
        "（[对照版](bilingual/ex/2024-03-05_carry-1.md)）"
    )
    assert entry in text
    # 组内新的在前
    assert text.index("2024-03-05_carry-1") < text.index("2023-01-01_c-3")
    assert "undated_d-4.md" in text


def test_build_index_empty() -> None:
    assert "共 0 篇" in build_index([])


def test_export_article_unknown_url(conn: sqlite3.Connection, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        export_article(conn, URL, SITE, GLOSSARY, tmp_path, None, ExportStats())
