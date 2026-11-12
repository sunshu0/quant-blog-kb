"""CFM 栏目接入：构造内容离线验证；真实样本只从被忽略的本地目录读取。"""

from collections import Counter
from pathlib import Path

import pytest
from lxml.html import document_fromstring

from qbkb import config, extract, store
from qbkb.models import DiscoveredUrl, Status
from qbkb.sections import (
    apply_sections,
    load_section_rules,
    new_sections,
    page_section,
    translatable,
)


def test_cfm_sections_start_unselected() -> None:
    rules = load_section_rules(config.load_site("cfm"))
    assert rules.enabled and len(rules.sections) == 4
    assert all(not s.collect for s in rules.sections)


def test_cfm_selection_and_new_section(tmp_path: Path) -> None:
    site = config.load_site("cfm")
    conn = store.connect(tmp_path / "state.db")
    keys = ["The Edge a CFM blog", "Commentary &amp; Insights", "New Research"]
    urls = []
    for i, key in enumerate(keys):
        url = f"https://www.cfm.com/sample-{i}/"
        urls.append(url)
        raw = tmp_path / f"sample-{i}.html"
        raw.write_text(
            "<html><head><title>Constructed research example</title>"
            '<meta property="article:published_time" content="2026-01-01"></head><body>'
            f'<section class="thinking-type-container"><h5>{key}</h5></section>'
            '<div class="content-blocks-container"><div class="col-80">'
            "<h1>Constructed research example</h1><p>"
            + "A made-up research paragraph for an offline test. " * 40
            + "</p></div></div></body></html>",
            encoding="utf-8",
        )
        store.upsert_discovered(conn, DiscoveredUrl(url, "cfm", f"sample-{i}"))
        store.set_status(conn, url, Status.FETCHED, raw_path=str(raw))
    stats = extract.run(conn, site)
    assert stats.failed == 0 and stats.extracted == 3
    for url in urls:
        row = store.get_article(conn, url)
        assert row is not None and row["status"] == Status.UNSELECTED
    assert translatable(conn, site) == []
    assert [s.key for s in new_sections(conn, site)] == ["NEW RESEARCH"]
    site.raw["sections"]["list"][0]["collect"] = True
    assert apply_sections(conn, site).reselected == 1
    assert translatable(conn, site) == [urls[0]]
    row = store.get_article(conn, urls[1])
    assert row is not None and row["section"] == "commentary-insights"
    conn.close()


SAMPLES = sorted((Path(__file__).resolve().parents[1] / "local_samples" / "cfm").glob("*.html"))


@pytest.mark.network
@pytest.mark.skipif(not SAMPLES, reason="没有本地 CFM 真实样本")
def test_local_cfm_pages_have_recognized_sections() -> None:
    # 不访问网络；标记与项目中真实页面检查保持一致，不随公开离线测试要求提供原文。
    rules = load_section_rules(config.load_site("cfm"))
    counts: Counter[str] = Counter()
    for path in SAMPLES:
        raw = path.read_text(encoding="utf-8")
        url = f"https://www.cfm.com/{path.stem}/"
        found = page_section(document_fromstring(raw), raw, url, rules)
        section = rules.classify(url, found.key)
        assert not found.issues and section is not None, path.name
        assert not section.collect
        counts[section.id] += 1
    assert len(SAMPLES) == 12
    assert counts == {
        "the-edge-a-cfm-blog": 3,
        "commentary-insights": 5,
        "academic-papers": 2,
        "whitepaper": 2,
    }
