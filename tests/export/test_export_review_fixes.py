"""PR #7 审查修复：Markdown 里不出现原始 HTML、链接地址转义、换 --out 重建不误删、
图片缓存和间隔下限。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.export import ImageFetcher, build_index, run, source_line, to_markdown
from qbkb.export.markdown import ArticleDoc, frontmatter_text, render, render_bilingual
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, translate_article


@pytest.mark.parametrize(
    ("html", "bad"),
    [
        ("<p>see &lt;script&gt;alert(1)&lt;/script&gt; here</p>", "<script>"),
        ('<p>&lt;img src=x onerror="alert(1)"&gt;</p>', "<img"),
        ("<p>y &lt;b&gt; <b>z</b></p>", "<b>"),
    ],
)
def test_escaped_html_in_text_stays_text(html: str, bad: str) -> None:
    md = to_markdown(html)
    assert bad not in md
    assert "&lt;" in md


def test_whitelist_and_code_are_kept() -> None:
    assert to_markdown("<p>x<sup>2</sup>, y<sub>t</sub></p>") == "x<sup>2</sup>, y<sub>t</sub>"
    assert to_markdown('<p><a href="https://e.test/x">https://e.test/x</a></p>') == (
        "<https://e.test/x>"
    )
    # 代码里 Markdown 不解析 HTML：原样保留，不能变成 &lt;
    assert to_markdown("<p><code>a &lt; b</code></p>") == "`a < b`"
    assert "if a < b:" in to_markdown("<pre><code>if a &lt; b:\n    pass</code></pre>")


def test_urls_with_parentheses_and_spaces() -> None:
    md = to_markdown(
        '<p><img src="https://e.test/a(1).png" alt="a"> <a href="https://e.test/p q">t</a></p>'
    )
    assert "![a](https://e.test/a%281%29.png)" in md
    assert "[t](https://e.test/p%20q)" in md


TITLE = "Why <script>alert(1)</script> & <b>bold</b> titles"


def test_titles_are_escaped_everywhere() -> None:
    line = source_line("Firm <x>", TITLE, "https://e.test/a(b)")
    assert "<script>" not in line and "<x>" not in line
    assert "<https://e.test/a%28b%29>" in line
    doc = ArticleDoc(
        frontmatter={"title": TITLE, "title_zh": "标题 <i>"},
        title_zh="标题 <i>",
        source_line=line,
        zh_blocks=["正文"],
        en_blocks=["Body"],
        n_body=1,
    )
    for text in (render(doc), render_bilingual(doc, TITLE)):
        body = text.split("---\n", 2)[2]
        assert "<script>" not in body and "<i>" not in body and "<b>" not in body
        assert "# 标题 &lt;i>" in body
    # frontmatter：含尖括号的值加双引号，内容原样可读回
    fm = frontmatter_text({"title": TITLE, "plain": "ok"})
    assert f'title: "{TITLE}"' in fm and "plain: ok" in fm
    assert yaml.safe_load(fm.split("---\n")[1])["title"] == TITLE


def test_index_escapes_titles(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "s.db")
    store.upsert_discovered(conn, DiscoveredUrl("https://e.test/a", "e", "a"))
    store.set_status(conn, "https://e.test/a", Status.PUBLISHED, title_zh="<img src=x onerror=y>")
    text = build_index(store.list_rows(conn, [Status.PUBLISHED]))
    conn.close()
    assert "<img" not in text and "&lt;img" in text


SITE = SiteConfig("ex", "allowed", {"base_url": "https://ex.test"})
URL = "https://ex.test/insights/a"
GLOSSARY = Glossary("1", {})


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    store.upsert_discovered(c, DiscoveredUrl(URL, "ex", "a"))
    store.replace_segments(c, URL, [Segment(0, "<p>Some made-up text.</p>")])
    store.set_status(c, URL, Status.FILTERED, title="A", published="2024-01-01")
    translate_article(c, URL, FakeTranslator(), GLOSSARY)
    yield c
    c.close()


def test_rebuild_into_other_out_dir_keeps_old_files(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    first, second = tmp_path / "out1", tmp_path / "out2"
    run(conn, SITE, GLOSSARY, first)
    old = first / "ex" / "2024-01-01_a.md"
    assert old.is_file()
    # 发布日期变了，又换了输出目录：旧目录里的文件不归这次管，不能删
    store.set_status(conn, URL, Status.PUBLISHED, published="2024-02-02")
    run(conn, SITE, GLOSSARY, second, rebuild=True)
    assert old.is_file() and (first / "bilingual" / "ex" / "2024-01-01_a.md").is_file()
    assert (second / "ex" / "2024-02-02_a.md").is_file()


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def _png(req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"PNG")


def test_real_site_interval_cannot_go_below_one_second(tmp_path: Path) -> None:
    real = SiteConfig("r", "allowed", {"base_url": "https://www.example.com"})
    clock = Clock()
    f = ImageFetcher(
        real,
        tmp_path,
        min_interval=0,
        transport=httpx.MockTransport(_png),
        sleep=clock.sleep,
        clock=clock,
    )
    assert f.min_interval == 1.0
    f.get("https://www.example.com/a.png")
    f.get("https://www.example.com/b.png")
    assert clock.sleeps == [1.0]
    local = ImageFetcher(SITE, tmp_path, min_interval=0, transport=httpx.MockTransport(_png))
    assert local.min_interval == 0


def test_existing_images_are_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from qbkb.export import image_stem

    (tmp_path / f"{image_stem('https://ex.test/old.png')}.png").write_bytes(b"old")
    listings = {"n": 0}
    real_iterdir = Path.iterdir

    def counting(self: Path) -> Iterator[Path]:
        listings["n"] += 1
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", counting)
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return _png(req)

    f = ImageFetcher(SITE, tmp_path, transport=httpx.MockTransport(handler), sleep=lambda s: None)
    for _ in range(3):  # 同一张图在主文件、对照版中英文里各处理一次
        f.get("https://ex.test/old.png")
        f.get("https://ex.test/new.png")
    assert hits == ["https://ex.test/new.png"]  # 已有的不请求，新的只下一次
    assert listings["n"] == 1  # 只扫了一次目录
