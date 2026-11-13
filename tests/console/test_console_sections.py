"""控制台「栏目抽样」页面：抽样表读写（和 CLI 同一个 CSV）、标注与占比、翻译缓存、接口保护。

全部用构造内容，不联网、不调用真实翻译后端。
"""

from __future__ import annotations

import csv
import json
import threading
import time
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
from qbkb.models import DiscoveredUrl, QuotaExceededError, Segment, Status, segment_hash
from qbkb.sections import survey
from qbkb.translate.fake import FakeTranslator
from qbkb.translate.glossary import Glossary

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


def _setup(root: Path, translator: Any = None) -> App:
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
    # 标两个要、一个不要 → 占比 66.7%：不到七成（保留一位小数，不四舍五入成 70% 引起矛盾）
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
    assert summary["article"]["占比"] == "66.7%"
    assert summary["article"]["结论"].startswith("不到七成")
    # 全要 → 超过七成；清除一个标记
    app.mark_section({"site": "demo", "url": urls[2], "mark": "要", "note": ""})
    view = app.sections_view("demo")
    summary = {r["栏目id"]: r for r in view["summary"]}
    assert summary["article"]["占比"] == "100.0%" and summary["article"]["结论"].startswith(
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


class GatedTranslator:
    """翻译时先发 started、再等 release，用来固定并发翻译的交错。"""

    name = "fake"

    def __init__(self) -> None:
        self._inner = FakeTranslator()
        self.started = threading.Event()
        self.release = threading.Event()

    def translate(self, segments: Any, glossary: Any, context: str = "") -> list[str]:
        self.started.set()
        self.release.wait(timeout=5)
        return self._inner.translate(segments, glossary, context)


def test_translate_section_rejects_concurrent_same_section(tmp_path: Path) -> None:
    gated = GatedTranslator()
    app = _setup(tmp_path, gated)
    app.generate_sections({"site": "demo"})
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["first"] = app.translate_section({"site": "demo", "section": "article"})
        except Exception as e:  # noqa: BLE001
            result["first"] = e

    t = threading.Thread(target=run)
    t.start()
    assert gated.started.wait(timeout=5)
    try:
        with pytest.raises(ApiError) as info:
            app.translate_section({"site": "demo", "section": "article"})
        assert "正在翻译" in info.value.message
    finally:
        gated.release.set()
    t.join(timeout=5)
    assert "first" in result and not isinstance(result["first"], Exception)


def test_translate_section_rejects_concurrent_different_sections(tmp_path: Path) -> None:
    """翻译记忆按文本共享，两个标签页翻不同栏目也可能撞同一段文本；App 级锁要一起拦住。"""
    gated = GatedTranslator()
    app = _setup(tmp_path, gated)
    app.generate_sections({"site": "demo"})
    result: dict[str, object] = {}

    def run() -> None:
        try:
            result["first"] = app.translate_section({"site": "demo", "section": "article"})
        except Exception as e:  # noqa: BLE001
            result["first"] = e

    t = threading.Thread(target=run)
    t.start()
    assert gated.started.wait(timeout=5)
    try:
        # 另一栏目同时翻也要被拒：整把锁，不让两个标签页各自翻同一段文本重复计费
        with pytest.raises(ApiError) as info:
            app.translate_section({"site": "demo", "section": "weekly"})
        assert "正在翻译" in info.value.message
    finally:
        gated.release.set()
    t.join(timeout=5)
    assert "first" in result and not isinstance(result["first"], Exception)


def test_concurrent_update_marks_preserves_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    path = _csv(tmp_path)
    urls = [r["网址"] for r in survey.read_csv(path)]
    orig = sections_ui._write_csv

    def slow_write(p: Path, rows: Any, fieldnames: Any) -> None:
        time.sleep(0.05)
        orig(p, rows, fieldnames)

    monkeypatch.setattr(sections_ui, "_write_csv", slow_write)
    result: dict[str, str] = {}

    def mark(url: str, mark: str) -> None:
        try:
            sections_ui.update_marks(path, {url: (mark, None)})
            result[url] = "ok"
        except Exception as e:  # noqa: BLE001
            result[url] = type(e).__name__

    t1 = threading.Thread(target=mark, args=(urls[0], "要"))
    t2 = threading.Thread(target=mark, args=(urls[1], "要"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert result[urls[0]] == "ok" and result[urls[1]] == "ok"
    by_url = {r["网址"]: r["要不要"] for r in survey.read_csv(path)}
    assert by_url[urls[0]] == "要" and by_url[urls[1]] == "要"


def test_write_csv_uses_unique_temp_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写回抽样表用唯一临时文件（不再固定 .csv.tmp），成功和失败后都不残留临时文件。"""
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    path = _csv(tmp_path)
    url = survey.read_csv(path)[0]["网址"]
    made: list[str] = []
    real_mkstemp = sections_ui.tempfile.mkstemp

    def spy(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        made.append(name)
        return fd, name

    monkeypatch.setattr(sections_ui.tempfile, "mkstemp", spy)
    sections_ui.update_marks(path, {url: ("要", None)})
    sections_ui.update_marks(path, {url: ("不要", None)})
    assert len(made) == 2 and made[0] != made[1]  # 两次写用不同临时文件
    assert all(not Path(name).name.endswith(".csv.tmp") for name in made)  # 不是固定名
    assert list(path.parent.glob("*.tmp")) == []  # 成功写入后不残留
    monkeypatch.undo()

    before = path.read_bytes()

    def fail_replace(self: Path, target: Path) -> Path:
        raise OSError("device busy")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(sections_ui.SamplingCsvError):
        sections_ui.update_marks(path, {url: ("要", None)})
    monkeypatch.undo()
    assert list(path.parent.glob("*.tmp")) == []  # 失败后也清理干净
    assert path.read_bytes() == before  # 原文件未变


class FailingAfterTranslator:
    """第一批成功、之后的批次抛额度用完。"""

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, segments: Any, glossary: Any, context: str = "") -> list[str]:
        self.calls += 1
        if self.calls >= 2:
            raise QuotaExceededError("额度用完")
        return ["<p>译</p>" for _ in segments]


def test_translate_units_keeps_earlier_batch_on_later_failure(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    glossary = Glossary("", {}, {})
    units = [f"<p>unit {i}</p>" for i in range(51)]  # 超过每批 50 条 → 两批
    translator = FailingAfterTranslator()
    with pytest.raises(QuotaExceededError):
        sections_ui.translate_units(conn, units, translator, glossary)
    assert translator.calls == 2
    # 第一批 50 条已缓存，第二批 1 条没缓存（重试只补那 1 条）
    cached = sum(
        1
        for u in units[:50]
        if store.tm_get(conn, segment_hash(u), "fake", glossary.version_for(u)) is not None
    )
    assert cached == 50
    assert (
        store.tm_get(conn, segment_hash(units[50]), "fake", glossary.version_for(units[50])) is None
    )
    conn.close()


class BadStructureTranslator:
    """返回非 <p> 结构，触发结构校验失败。"""

    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def translate(self, segments: Any, glossary: Any, context: str = "") -> list[str]:
        self.calls += 1
        return ["<div>坏结构</div>" for _ in segments]


def test_translate_units_negative_caches_structure_failures(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    glossary = Glossary("", {}, {})
    units = ["<p>title</p>", "<p>summary</p>"]
    translator = BadStructureTranslator()
    done, failed = sections_ui.translate_units(conn, units, translator, glossary)
    assert done == 0 and len(failed) == 2
    # 负缓存：再翻不再调用后端、也不报失败（missing_units 跳过 rejected）
    done2, failed2 = sections_ui.translate_units(conn, units, translator, glossary)
    assert done2 == 0 and failed2 == []
    assert translator.calls == 1
    conn.close()


def test_update_marks_preserves_extra_columns_and_order(tmp_path: Path) -> None:
    app = _setup(tmp_path)
    app.generate_sections({"site": "demo"})
    path = _csv(tmp_path)
    rows = survey.read_csv(path)
    fieldnames = ["备注", "网址", "要不要", "审批人", "栏目id"]  # 重排 + 加一列
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            row = {k: r.get(k, "") for k in fieldnames}
            row["审批人"] = "Alice"
            w.writerow(row)
    url = rows[0]["网址"]
    sections_ui.update_marks(path, {url: ("要", "备注X")})
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        new_fieldnames = list(reader.fieldnames or [])
        new_rows = list(reader)
    assert new_fieldnames == fieldnames  # 原列顺序保留
    by_url = {r["网址"]: r for r in new_rows}
    assert by_url[url]["要不要"] == "要" and by_url[url]["备注"] == "备注X"
    assert by_url[url]["审批人"] == "Alice"  # 多余列的值没丢


def test_saving_key_rebuilds_translator(tmp_path: Path) -> None:
    calls: list[int] = []

    def factory(ws: Any) -> FakeTranslator:
        calls.append(1)
        return FakeTranslator()

    app = App(tmp_path, token=TOKEN, translator_factory=factory)
    app.save_keys({"deepl": "0123abcd-1111-2222-3333-444455556666:fx"})
    t1 = app._translator(app.ws())
    assert len(calls) == 1
    # 换 key → 重建翻译器，用新凭据
    app.save_keys({"deepl": "0123abcd-1111-2222-3333-444455557777:fx"})
    t2 = app._translator(app.ws())
    assert len(calls) == 2 and t1 is not t2
    # 清空 key → 模式回 demo，也重建
    app.save_keys({"clear": ["deepl"]})
    app._translator(app.ws())
    assert len(calls) == 3


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
