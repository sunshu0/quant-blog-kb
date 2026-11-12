"""控制台「栏目抽样」页面：抽样表读写（和 CLI 同一个 CSV）、标注与占比、翻译缓存、接口保护。

全部用构造内容，不联网、不调用真实翻译后端。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from qbkb import store
from qbkb.console import sections_ui, server
from qbkb.console.server import ApiError, App
from qbkb.console.workspace import workspace
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.sections import survey
from qbkb.translate.fake import FakeTranslator

TOKEN = "test-token-123"

# 构造的站点配置：两个栏目，都用 key 识别（和 sections 测试里的规则一致）
_SECTIONS = {
    "page_key": {"selector": "#tagline p"},
    "list": [
        {"id": "article", "name": "Articles", "key": "ARTICLE", "collect": True},
        {"id": "weekly", "name": "Weekly Notes", "key": "ARTICLE | WEEKLY NOTES", "collect": False},
    ],
}

_SITE_RAW: dict[str, Any] = {
    "compliance": {"decision": "allowed"},
    "base_url": "https://blog.example.test",
    "discover": {"sitemap": "https://blog.example.test/sitemap.xml"},
    "sections": _SECTIONS,
}


def _add(
    conn: Any,
    url: str,
    key: str,
    title: str,
    *,
    segments: bool = True,
    published: str = "2024-01-01",
) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url, "demo", url.rstrip("/").rsplit("/", 1)[-1]))
    store.set_status(conn, url, Status.EXTRACTED, section_key=key, title=title, published=published)
    if segments:
        store.replace_segments(
            conn, url, [Segment(0, f"<p>Body of {title} with words for a summary.</p>")]
        )


def _setup(root: Path, translator: FakeTranslator | None = None) -> App:
    ws = workspace(root, "demo")
    ws.base.mkdir(parents=True, exist_ok=True)
    cfg = ws.base / "config"
    (cfg / "sites").mkdir(parents=True, exist_ok=True)
    (cfg / "sites" / "demo.yaml").write_text(
        yaml.safe_dump(_SITE_RAW, allow_unicode=True), encoding="utf-8"
    )
    conn = store.connect(ws.db)
    for i in range(3):
        _add(conn, f"https://blog.example.test/insights/art-{i}", "ARTICLE", f"Article {i}")
    for i in range(2):
        _add(
            conn,
            f"https://blog.example.test/insights/wk-{i}",
            "ARTICLE | WEEKLY NOTES",
            f"Weekly {i}",
        )
    conn.close()
    factory = (lambda _ws: translator) if translator is not None else None
    return App(root, token=TOKEN, translator_factory=factory)


def _csv(root: Path) -> Path:
    return workspace(root, "demo").out / "sections" / "demo.csv"


def test_generate_writes_bom_and_column_order(tmp_path: Path) -> None:
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    path = _csv(tmp_path)
    assert path.is_file()
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # UTF-8 带 BOM
    rows = survey.read_csv(path)
    assert list(rows[0].keys()) == list(survey.COLUMNS)  # 列顺序和 CLI 一致
    by = {r["栏目id"]: 0 for r in rows}
    for r in rows:
        by[r["栏目id"]] += 1
    assert by == {"article": 3, "weekly": 2}  # 不超过 30 篇的栏目列全部


def test_sections_view_without_site_defaults_to_first(tmp_path: Path) -> None:
    app = _setup(tmp_path)
    view: Any = app.sections_view("")
    assert view["site"] == "demo" and view["sites"] == ["demo"]


def test_mark_roundtrip_and_verdict_bands(tmp_path: Path) -> None:
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    urls = [r["网址"] for r in survey.read_csv(_csv(tmp_path)) if r["栏目id"] == "article"]
    # 标两个要、一个不要 → 占比 67%：不到七成
    app.mark_section({"site": "demo", "url": urls[0], "mark": "要", "note": ""})
    app.mark_section({"site": "demo", "url": urls[1], "mark": "要", "note": "备注1"})
    app.mark_section({"site": "demo", "url": urls[2], "mark": "不要", "note": ""})
    rows = survey.read_csv(_csv(tmp_path))
    marks = {r["网址"]: r for r in rows if r["栏目id"] == "article"}
    assert marks[urls[0]]["要不要"] == "要" and marks[urls[1]]["备注"] == "备注1"
    assert marks[urls[2]]["要不要"] == "不要"
    view: Any = app.sections_view("demo")
    summary = {r["栏目id"]: r for r in view["summary"]}
    assert summary["article"]["已标"] == "3" and summary["article"]["要"] == "2"
    assert summary["article"]["占比"] == "67%"
    assert summary["article"]["结论"].startswith("不到七成")
    # 全要 → 超过七成；清除一个标记
    app.mark_section({"site": "demo", "url": urls[2], "mark": "要", "note": ""})
    view = app.sections_view("demo")
    summary = {r["栏目id"]: r for r in view["summary"]}
    assert summary["article"]["占比"] == "100%" and summary["article"]["结论"].startswith(
        "超过七成"
    )
    app.mark_section({"site": "demo", "url": urls[0], "mark": "", "note": ""})
    rows = survey.read_csv(_csv(tmp_path))
    assert [r for r in rows if r["网址"] == urls[0]][0]["要不要"] == ""


def test_mark_rejects_unknown_url_and_bad_mark(tmp_path: Path) -> None:
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    with pytest.raises(ApiError) as info:
        app.mark_section({"site": "demo", "url": "https://blog.example.test/nope", "mark": "要"})
    assert "不在抽样表" in info.value.message
    with pytest.raises(ApiError):
        app.mark_section({"site": "demo", "url": "x", "mark": "maybe"})
    with pytest.raises(ApiError):
        app.mark_section({"site": "demo", "url": "x", "mark": "要", "note": 123})


def test_write_failure_keeps_old_marks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    url = survey.read_csv(_csv(tmp_path))[0]["网址"]
    app.mark_section({"site": "demo", "url": url, "mark": "要", "note": ""})
    before = _csv(tmp_path).read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("device busy")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(sections_ui.SamplingCsvError) as info:
        sections_ui.update_marks(_csv(tmp_path), {url: ("不要", "")})
    assert "写不回去" in str(info.value)
    monkeypatch.undo()
    assert _csv(tmp_path).read_bytes() == before  # 原来的标记没丢


def test_translate_section_caches_and_does_not_repeat(tmp_path: Path) -> None:
    translator = FakeTranslator()
    app = _setup(tmp_path, translator)
    app.generate_sections({"site": "demo"})
    first: Any = app.translate_section({"site": "demo", "section": "article"})
    calls = len(translator.calls)
    assert calls >= 1
    arts = next(s for s in first["sections"] if s["id"] == "article")["articles"]
    assert all(a["title_zh"] and a["summary_zh"] for a in arts)
    # 再翻一次：翻译记忆命中，不再调用后端
    app.translate_section({"site": "demo", "section": "article"})
    assert len(translator.calls) == calls
    assert translator.chars_used > 0


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[App, httpx.Client]]:
    app = _setup(tmp_path)
    srv = server.make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5)
    try:
        yield app, client
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()


def test_sections_endpoints_require_token(served: tuple[App, httpx.Client]) -> None:
    _, client = served
    body = {"site": "demo", "section": "article"}
    for path in ("/api/sections/generate", "/api/sections/mark", "/api/sections/translate"):
        r = client.post(
            path, content=json.dumps(body), headers={"Content-Type": "application/json"}
        )
        assert r.status_code == 403, path
    # 带 token、同源就能过
    r = client.post(
        "/api/sections/generate",
        content=json.dumps({"site": "demo"}),
        headers={"Content-Type": "application/json", "X-QBKB-Token": TOKEN},
    )
    assert r.status_code == 200
