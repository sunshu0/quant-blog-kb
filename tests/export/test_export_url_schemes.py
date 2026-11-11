"""PR #7 审查修复（第 2 轮）：export 自己再拦一道危险链接协议（纵深防御）。"""

import sqlite3
from pathlib import Path

import httpx
import pytest

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.export import ImageFetcher, run, source_line, to_markdown
from qbkb.export.markdown import url_allowed
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, translate_article

BAD = [
    "javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "java\tscript:alert(1)",
    "java&#x09;script:alert(1)",
    " \x01javascript:alert(1)",
    "&#106;avascript:alert(1)",
    "vbscript:msgbox(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "file:///etc/passwd",
]


@pytest.mark.parametrize("url", BAD)
def test_bad_link_keeps_text_only(url: str) -> None:
    html = f'<p>see <a href="{url}">bad link</a> here</p>'
    md = to_markdown(html)
    assert md == "see bad link here"
    assert not url_allowed(url, "link")


@pytest.mark.parametrize("url", BAD)
def test_bad_image_keeps_alt_only(url: str) -> None:
    md = to_markdown(f'<p><img src="{url}" alt="A &lt;chart&gt;"> after</p>')
    assert md == "A &lt;chart> after"
    assert "](" not in md and "<chart>" not in md
    assert not url_allowed(url, "image")


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<p><a href="https://e.test/a">t</a></p>', "[t](https://e.test/a)"),
        ('<p><a href="http://e.test/a">t</a></p>', "[t](http://e.test/a)"),
        ('<p><a href="mailto:me@e.test">mail</a></p>', "[mail](mailto:me@e.test)"),
        ('<p><a href="#fn1">1</a></p>', "[1](#fn1)"),
        ('<p><a href="../glossary#spread">g</a></p>', "[g](../glossary#spread)"),
        ('<p><img src="https://e.test/a.png" alt="a"></p>', "![a](https://e.test/a.png)"),
        ('<p><img src="images/a.png" alt="a"></p>', "![a](images/a.png)"),
        ('<p><img src="../../ex/images/a.png" alt="a"></p>', "![a](../../ex/images/a.png)"),
    ],
)
def test_good_urls_kept(html: str, expected: str) -> None:
    assert to_markdown(html) == expected


def test_image_rules_are_stricter_than_links() -> None:
    assert url_allowed("mailto:me@e.test", "link")
    assert not url_allowed("mailto:me@e.test", "image")
    assert not url_allowed("#x", "image")


def test_source_url_checked() -> None:
    assert "<https://e.test/a>" in source_line("F", "T", "https://e.test/a")
    line = source_line("F", "T", "javascript:alert(1)")
    assert "javascript" not in line and line.startswith("> 来源：F《T》。")


def test_export_strips_bad_links_in_both_versions(tmp_path: Path) -> None:
    site = SiteConfig("ex", "allowed", {"base_url": "https://ex.test"})
    url = "https://ex.test/insights/a"
    conn: sqlite3.Connection = store.connect(tmp_path / "s.db")
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", "a"))
    paras = [
        '<p>Read <a href="JaVaScRiPt:alert(1)">this</a> and <a href="https://ex.test/b">that</a>.</p>',
        '<p><img src="data:image/png;base64,AAAA" alt="Inline chart"></p>',
    ]
    store.replace_segments(conn, url, [Segment(i, h) for i, h in enumerate(paras)])
    store.set_status(conn, url, Status.FILTERED, title="T", published="2024-01-01")
    translate_article(conn, url, FakeTranslator(), Glossary("1", {}))
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(404)

    out = tmp_path / "out"
    images = ImageFetcher(site, out / "ex" / "images", transport=httpx.MockTransport(handler))
    stats = run(conn, site, Glossary("1", {}), out, images=images)
    conn.close()
    assert stats.published == 1 and stats.image_failures == [] and hits == []
    for path in (out / "ex" / "2024-01-01_a.md", out / "bilingual" / "ex" / "2024-01-01_a.md"):
        text = path.read_text(encoding="utf-8")
        assert "javascript" not in text.lower() and "data:image" not in text
        assert "](https://ex.test/b)" in text
        assert "Inline chart" in text
        assert "source_url: https://ex.test/insights/a" in text
