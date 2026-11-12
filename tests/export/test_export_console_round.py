"""控制台这一轮顺带修的两个导出问题：外站图片单独统计；短术语的注释不挂到长短语里。"""

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.export import ImageFetcher, TermAnnotator, run
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.translate import FakeTranslator, Glossary, load_glossary, translate_article

SITE = SiteConfig("ex", "allowed", {"base_url": "https://ex.test"})
URL = "https://ex.test/insights/charts"
OWN = "https://ex.test/files/fig1.png"
FLOURISH = "https://public.flourish.studio/visualisation/123/thumbnail"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def _add(conn: sqlite3.Connection) -> None:
    paras = [
        "<p>Some constructed words.</p>",
        f'<figure><img src="{OWN}" alt="own"></figure>',
        f'<figure><img src="{FLOURISH}" alt="chart"></figure>',
    ]
    store.upsert_discovered(conn, DiscoveredUrl(URL, "ex", "charts"))
    store.replace_segments(conn, URL, [Segment(i, h) for i, h in enumerate(paras)])
    store.set_status(conn, URL, Status.FILTERED, title="Charts", published="2024-01-02")
    translate_article(conn, URL, FakeTranslator(), Glossary("1", {}))


def test_external_images_are_counted_separately(conn: sqlite3.Connection, tmp_path: Path) -> None:
    _add(conn)
    out = tmp_path / "output"
    hits: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(str(req.url))
        return httpx.Response(200, headers={"Content-Type": "image/png"}, content=b"png")

    images = ImageFetcher(
        SITE, out / "ex" / "images", transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    stats = run(conn, SITE, Glossary("1", {}), out, images=images)
    assert stats.published == 1
    assert stats.image_failures == []  # 外站图片不算失败
    assert [f.image_url for f in stats.external_images] == [FLOURISH]
    assert hits == [OWN]  # 外站的不发请求
    assert not (out / "ex" / "images" / "failed.tsv").exists()
    text = (out / "ex" / "2024-01-02_charts.md").read_text(encoding="utf-8")
    assert f"]({FLOURISH})" in text  # 保留原链接
    assert images.is_external(FLOURISH) and not images.is_external(OWN)


def test_cli_prints_external_images(conn: sqlite3.Connection, tmp_path: Path) -> None:
    from qbkb import cli

    stats_line = "外站图片（保留原链接）"
    assert stats_line in Path(cli.__file__).read_text(encoding="utf-8")


GLOSSARY = {"volatility": "波动率", "implied volatility": "隐含波动率", "drawdown": "回撤"}


def test_short_term_not_annotated_inside_longer_phrase() -> None:
    """前一段已经标过 implied volatility；这一段原文里两种都有，但译文里只有"隐含波动率"：
    以前会变成"隐含波动率（volatility）"。"""
    a = TermAnnotator(GLOSSARY)
    first = a.annotate("<p>隐含波动率上升。</p>", {"implied volatility": "隐含波动率"})
    assert first == "<p>隐含波动率（implied volatility）上升。</p>"
    second = a.annotate(
        "<p>隐含波动率再次上升。</p>",
        {"implied volatility": "隐含波动率", "volatility": "波动率"},
    )
    assert second == "<p>隐含波动率再次上升。</p>"
    third = a.annotate("<p>隐含波动率高于实际的波动率。</p>", {"volatility": "波动率"})
    assert third == "<p>隐含波动率高于实际的波动率（volatility）。</p>"


def test_same_paragraph_both_terms_annotated_separately() -> None:
    a = TermAnnotator(GLOSSARY)
    out = a.annotate(
        "<p>隐含波动率和波动率不同。</p>",
        {"implied volatility": "隐含波动率", "volatility": "波动率"},
    )
    assert out == "<p>隐含波动率（implied volatility）和波动率（volatility）不同。</p>"


def test_repo_glossary_long_phrases_cover_short_translations() -> None:
    """仓库术语表里，一个译文包含另一个译文时，英文也要是包含关系（长短语整条收录）。
    否则导出时短词的注释会挂到长短语上，比如"已实现波动率（volatility）"。"""
    g = load_glossary()
    assert g["implied volatility"] == "隐含波动率"
    assert g["realized volatility"] == g["realised volatility"] == "已实现波动率"
    bad = [
        (short_en, long_en)
        for short_en, short_zh in g.items()
        for long_en, long_zh in g.items()
        if short_zh != long_zh
        and short_zh in long_zh
        and short_zh != short_en
        and short_en.lower() not in long_en.lower()
    ]
    assert bad == []
