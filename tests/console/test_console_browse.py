"""控制台浏览页 v2：站点 → 栏目分组、文章新字段、POST /api/translate（翻选中文章全文）。

全程离线：状态库在临时目录，子进程用假的，翻译用不到网络（dry_run 只读状态库）。
"""

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from qbkb import store
from qbkb.console import browse, jobs, server
from qbkb.console.server import REAL_BACKEND, ApiError, App
from qbkb.console.workspace import workspace
from qbkb.models import DiscoveredUrl, Segment, Status, segment_hash

TOKEN = "test-token-123"
DEEPL = "0123abcd-1111-2222-3333-444455556666:fx"


class FakeProc:
    def __init__(self, gate: threading.Event | None = None) -> None:
        self.pid = 4242
        self.stdout: Any = self
        self._gate = gate
        self.finished = False

    def __iter__(self) -> Any:
        if self._gate is not None:
            self._gate.wait(5)
        return iter(["[translate] ok\n"])

    def wait(self) -> int:
        self.finished = True
        return 0

    def poll(self) -> int | None:
        return 0 if self.finished else None

    def terminate(self) -> None:
        pass


class SpyPopen:
    """记录被启动的命令；设了 gate 时子进程一直卡到 gate 放开（模拟"任务正在跑"）。"""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.calls: list[list[str]] = []
        self.gate = gate

    def __call__(self, cmd: list[str], **_kw: Any) -> FakeProc:
        self.calls.append(cmd)
        return FakeProc(self.gate)


def _real_app(root: Path, popen: SpyPopen | None = None) -> App:
    (root / ".env").write_text(f"DEEPL_API_KEY={DEEPL}\n", encoding="utf-8")
    app = App(
        root,
        token=TOKEN,
        popen=popen or SpyPopen(),
        environ={"PATH": "x"},
        is_windows=False,
        kill=lambda p: p.terminate(),
    )
    app.set_mode({"mode": "real"})
    return app


def _add(
    conn: Any,
    site: str,
    slug: str,
    *,
    status: Status = Status.PUBLISHED,
    section: str | None = None,
    published: str | None = "2024-01-02",
    title_zh: str | None = "中文标题",
    translated: bool = False,
    summary_zh: str | None = "这是中文摘要。",
    pdf: bool = False,
    qc: list[str] | None = None,
    body: str = "Some English paragraph for the article.",
) -> str:
    url = f"https://{site}.example.test/{slug}/"
    store.upsert_discovered(conn, DiscoveredUrl(url, site, slug))
    fields: dict[str, Any] = {
        "title": f"Title {slug}",
        "title_zh": title_zh,
        "published": published,
        "section": section,
        "summary_zh": summary_zh,
        "pdf_path": f"raw/{slug}.pdf" if pdf else None,
        "qc_issues": json.dumps(qc, ensure_ascii=False) if qc else None,
    }
    if translated:
        fields["translated_at"] = "2026-11-12T00:00:00+00:00"
        fields["translator"] = "deepl:ZH-HANS"
    store.set_status(conn, url, status, **fields)
    store.replace_segments(conn, url, [Segment(0, f"<p>{body}</p>")])
    return url


def _seed_real(root: Path) -> dict[str, str]:
    conn = store.connect(root / "state.db")
    urls = {
        # cfm 配置里栏目顺序：the-edge… commentary-insights, academic-papers, whitepaper
        "wp_old": _add(conn, "cfm", "wp-old", section="whitepaper", published="2023-05-01"),
        "wp_new": _add(
            conn,
            "cfm",
            "wp-new",
            section="whitepaper",
            published="2024-06-01",
            translated=True,
            pdf=True,
            qc=["正文太短：12 字符（下限 200）", "PDF 正文：公式、表格按原文文字提取，可能不完整"],
        ),
        "ap": _add(conn, "cfm", "ap", section="academic-papers", published=None),
        "unknown": _add(conn, "cfm", "unk", section="zzz-new", title_zh=None),
        "man_art": _add(conn, "man", "man-art", section="article", summary_zh=None),
        "failed": _add(conn, "cfm", "bad", status=Status.FAILED, section="whitepaper"),
    }
    conn.close()
    return urls


# ---- 1. /api/articles：sites ----------------------------------------------------


def test_sites_grouped_in_config_order_and_old_groups_kept(tmp_path: Path) -> None:
    app = _real_app(tmp_path)
    _seed_real(tmp_path)
    data = app.articles()
    assert data["total"] == 5  # failed 的不算
    sites: Any = data["sites"]
    assert [s["site"] for s in sites] == ["cfm", "man"]  # 站点按配置顺序
    cfm = sites[0]
    assert cfm["name"] == "CFM" and cfm["total"] == 4
    # 栏目按配置顺序（academic-papers 在 whitepaper 前），未知栏目最后
    assert [s["id"] for s in cfm["sections"]] == ["academic-papers", "whitepaper", "zzz-new"]
    assert [s["total"] for s in cfm["sections"]] == [1, 2, 1]
    assert cfm["sections"][1]["name"] == "白皮书（Whitepaper）"  # 中文在前，英文括号在后
    assert cfm["sections"][2]["name"] == "zzz-new"  # 未知栏目没有名字就显示 id
    wp = cfm["sections"][1]["articles"]
    assert [a["date"] for a in wp] == ["2024-06-01", "2023-05-01"]  # 日期倒序
    assert sites[1]["name"] == "Man Group" and sites[1]["sections"][0]["id"] == "article"
    # 旧结构还在，字段不变
    groups: Any = data["groups"]
    assert {"category", "articles"} <= set(groups[0])
    assert set(groups[0]["articles"][0]) == {"url", "title", "title_en", "date"}
    assert sum(len(g["articles"]) for g in groups) == 5


def test_card_fields(tmp_path: Path) -> None:
    app = _real_app(tmp_path)
    urls = _seed_real(tmp_path)
    sites: Any = app.articles()["sites"]
    cards = {a["url"]: a for s in sites for sec in s["sections"] for a in sec["articles"]}
    new = cards[urls["wp_new"]]
    assert new["title"] == "中文标题" and new["title_en"] == "Title wp-new"
    assert new["summary"] == "这是中文摘要。" and new["translated"] is True
    assert new["has_pdf"] is True and new["date"] == "2024-06-01"
    assert new["flags"] == ["正文太短", "PDF 公式表格可能不完整"]
    old = cards[urls["wp_old"]]
    assert old["translated"] is False and old["has_pdf"] is False and old["flags"] == []
    assert cards[urls["ap"]]["date"] is None
    assert cards[urls["unknown"]]["title"] == "Title unk"  # 缺中文标题用英文
    assert cards[urls["man_art"]]["summary"] is None


def test_flag_labels_are_short_deduped_and_capped() -> None:
    issues = json.dumps(
        [
            "正文太短：1 字符",
            "正文太短：2 字符",
            "缺少标题",
            "缺少发布日期",
            "出现噪音关键词 'x'：段落 3",
        ],
        ensure_ascii=False,
    )
    assert browse.flag_labels(issues) == ["正文太短", "缺少标题", "缺少发布日期"]
    assert browse.flag_labels(None) == [] and browse.flag_labels("") == []
    assert browse.flag_labels("不是 JSON：x") == ["不是 JSON"]
    assert len(browse.flag_labels(json.dumps(["x" * 100]))[0]) == browse.MAX_FLAG_CHARS


def test_demo_workspace_only_shows_demo_site(tmp_path: Path) -> None:
    app = App(tmp_path, token=TOKEN, popen=SpyPopen())
    ws = workspace(tmp_path, "demo")
    ws.base.mkdir(parents=True)
    conn = store.connect(ws.db)
    _add(conn, "demo", "d1", section=None)
    _add(conn, "other", "o1", section=None)
    conn.close()
    sites: Any = app.articles()["sites"]
    assert [s["site"] for s in sites] == ["demo"]  # ws.site 有值：只含那一个站点
    assert sites[0]["total"] == 1


def test_empty_workspace_has_empty_sites(tmp_path: Path) -> None:
    app = App(tmp_path, token=TOKEN, popen=SpyPopen())
    data = app.articles()
    assert data["sites"] == [] and data["groups"] == []


# ---- 2. /api/article 新字段 ------------------------------------------------------


def test_article_has_summary_translated_and_pdf(tmp_path: Path) -> None:
    from qbkb.export import article_paths

    app = _real_app(tmp_path)
    urls = _seed_real(tmp_path)
    ws = workspace(tmp_path, "real")
    for key, slug in (("wp_new", "wp-new"), ("man_art", "man-art")):
        site = "cfm" if key == "wp_new" else "man"
        main, _ = article_paths(
            ws.out, site, "2024-01-02" if key != "wp_new" else "2024-06-01", slug
        )
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_text("# 标题\n\n摘要\n", encoding="utf-8")
    a = app.article(urls["wp_new"], "zh")
    assert a["summary"] == "这是中文摘要。" and a["translated"] is True and a["has_pdf"] is True
    b = app.article(urls["man_art"], "zh")
    assert b["summary"] is None and b["translated"] is False and b["has_pdf"] is False
    assert "html" in b and b["title"] == "中文标题"  # 原有字段不变


# ---- 3. POST /api/translate ------------------------------------------------------


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[App, httpx.Client, SpyPopen]]:
    popen = SpyPopen()
    app = _real_app(tmp_path, popen)
    srv = server.make_server(app, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5)
    try:
        yield app, client, popen
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()
        app.close()


def _post(client: httpx.Client, body: object, **headers: str) -> httpx.Response:
    h = {"Content-Type": "application/json", "X-QBKB-Token": TOKEN, **headers}
    return client.post("/api/translate", content=json.dumps(body), headers=h)


def test_translate_needs_token_same_origin_and_json(
    served: tuple[App, httpx.Client, SpyPopen], tmp_path: Path
) -> None:
    _, client, popen = served
    urls = _seed_real(tmp_path)
    body = {"urls": [urls["wp_old"]], "dry_run": False}
    no_token = client.post(
        "/api/translate", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert no_token.status_code == 403
    assert _post(client, body, **{"X-QBKB-Token": "wrong"}).status_code == 403
    assert _post(client, body, Origin="https://evil.example").status_code == 403
    form = client.post(
        "/api/translate",
        content=json.dumps(body),
        headers={"Content-Type": "text/plain", "X-QBKB-Token": TOKEN},
    )
    assert form.status_code == 415
    assert client.get("/api/translate").status_code == 404  # GET 没有这个接口
    assert popen.calls == []  # 校验没过，一个子进程都没起
    ok_origin = _post(client, {**body, "dry_run": True}, Origin=str(client.base_url))
    assert ok_origin.status_code == 200


def test_translate_validation(served: tuple[App, httpx.Client, SpyPopen], tmp_path: Path) -> None:
    _, client, popen = served
    urls = _seed_real(tmp_path)
    ok = urls["wp_old"]
    assert _post(client, {"urls": [ok]}).status_code == 400  # 缺 dry_run
    assert _post(client, {"urls": [ok], "dry_run": "yes"}).status_code == 400
    assert _post(client, {"urls": [], "dry_run": True}).status_code == 400
    assert _post(client, {"urls": "x", "dry_run": True}).status_code == 400
    assert _post(client, {"urls": [1], "dry_run": True}).status_code == 400
    r = _post(client, {"urls": [urls["failed"]], "dry_run": True})  # 不是 published
    assert r.status_code == 400 and "已发布" in r.json()["error"]
    assert _post(client, {"urls": ["https://nope.test/"], "dry_run": True}).status_code == 400
    # 一个不合格就整体拒绝，不会只翻合格的
    assert _post(client, {"urls": [ok, urls["failed"]], "dry_run": False}).status_code == 400
    many = [f"https://cfm.example.test/{i}/" for i in range(51)]
    r = _post(client, {"urls": many, "dry_run": True})
    assert r.status_code == 400 and r.json()["error"] == "一次最多选 50 篇"
    assert popen.calls == []


def test_translate_dedupes_before_counting(
    served: tuple[App, httpx.Client, SpyPopen], tmp_path: Path
) -> None:
    _, client, _ = served
    urls = _seed_real(tmp_path)
    ok = urls["wp_old"]
    # 同一篇重复 60 次：去重后是 1 篇，不触发 50 篇上限
    r = _post(client, {"urls": [ok] * 60, "dry_run": True})
    assert r.status_code == 200 and r.json()["count"] == 1


def test_dry_run_counts_already_and_estimates_without_external_calls(
    served: tuple[App, httpx.Client, SpyPopen],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client, popen = served
    urls = _seed_real(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("dry_run 不能调外部服务")

    import deepl

    monkeypatch.setattr(deepl, "DeepLClient", boom)
    stub = SimpleNamespace(Client=boom, get=boom, HTTPError=httpx.HTTPError)
    monkeypatch.setattr(server, "httpx", stub)  # 控制台自己发的 httpx 请求（测 key 等）也不能有
    ws = app.ws()
    glossary = app._glossary(ws)  # pyright: ignore[reportPrivateUsage]
    # 标题（已有中文标题但标题单元没进翻译记忆）+ 正文都未命中
    r = _post(client, {"urls": [urls["wp_old"], urls["wp_new"]], "dry_run": True})
    data = r.json()
    assert r.status_code == 200
    assert data["count"] == 1 and data["already"] == 1  # wp_new 已翻
    assert data["backend"] == "deepl" and data["quota_left"] is None
    full = data["chars"]
    # 有待翻正文段落：chars 把"校验失败重翻一次"也算进去（上限），chars_base 是不含重翻的基础值
    assert full > data["chars_base"] > 0 and data["chars_upper_bound"] is True
    # 正文段落进翻译记忆后，估算减少
    conn = store.connect(tmp_path / "state.db")
    unit = "<p>Some English paragraph for the article.</p>"
    store.tm_put(
        conn,
        segment_hash(unit),
        REAL_BACKEND,
        unit,
        "<p>一段中文。</p>",
        glossary.version_for(unit),
    )
    conn.close()
    est = _post(client, {"urls": [urls["wp_old"]], "dry_run": True}).json()
    # 正文命中后只剩标题（标题不重试）：数字是确切的，不再是上限
    assert est["chars"] == est["chars_base"] > 0 and est["chars"] < full
    assert est["chars_upper_bound"] is False
    # 额度来自上次记录（quota.json），没有就是 null
    (tmp_path / ".qbkb" / "quota.json").write_text(
        json.dumps({"used": 1, "limit": 500000, "remaining": 499999}), encoding="utf-8"
    )
    assert _post(client, {"urls": [urls["wp_old"]], "dry_run": True}).json()["quota_left"] == 499999
    assert popen.calls == []
    assert not list((tmp_path / ".qbkb").glob("translate-urls-*.txt"))  # dry_run 不写清单


def test_dry_run_all_translated_reports_zero(
    served: tuple[App, httpx.Client, SpyPopen], tmp_path: Path
) -> None:
    _, client, popen = served
    urls = _seed_real(tmp_path)
    data = _post(client, {"urls": [urls["wp_new"]], "dry_run": True}).json()
    assert (data["count"], data["already"], data["chars"]) == (0, 1, 0)
    # 真跑时全部已翻：没有可做的，不起任务
    assert _post(client, {"urls": [urls["wp_new"]], "dry_run": False}).status_code == 400
    assert popen.calls == []


def test_real_run_starts_job_with_url_file_then_export(
    served: tuple[App, httpx.Client, SpyPopen], tmp_path: Path
) -> None:
    app, client, popen = served
    urls = _seed_real(tmp_path)
    body = {"urls": [urls["wp_old"], urls["wp_new"], urls["man_art"]], "dry_run": False}
    r = _post(client, body)
    assert r.status_code == 200 and r.json() == {"started": True, "count": 2}
    app.jobs.wait(10)
    assert len(popen.calls) == 2
    tr, ex = popen.calls
    assert tr[3] == "translate" and ex[3] == "export"
    listing = Path(tr[tr.index("--url-file") + 1])
    assert listing.parent == tmp_path / ".qbkb"
    assert listing.read_text(encoding="utf-8").splitlines() == [urls["wp_old"], urls["man_art"]]
    assert "--site" not in tr and "--retry-rejected" not in tr
    job = client.get("/api/job").json()["job"]
    assert job["kind"] == "translate" and job["exit_code"] == 0


def test_real_run_conflicts_while_a_job_is_running(tmp_path: Path) -> None:
    gate = threading.Event()
    popen = SpyPopen(gate)
    app = _real_app(tmp_path, popen)
    urls = _seed_real(tmp_path)
    try:
        first = app.translate_fulltext({"urls": [urls["wp_old"]], "dry_run": False})
        assert first == {"started": True, "count": 1}
        with pytest.raises(ApiError) as info:
            app.translate_fulltext({"urls": [urls["man_art"]], "dry_run": False})
        assert info.value.status == 409
        with pytest.raises(ApiError) as info:
            app.start_run({"limit": 1})  # 和 /api/run 共用同一把任务锁
        assert info.value.status == 409
        # 估算不用任务锁：任务在跑时照样能估
        assert app.translate_fulltext({"urls": [urls["man_art"]], "dry_run": True})["count"] == 1
        leftovers = list((tmp_path / ".qbkb").glob("translate-urls-*.txt"))
        assert len(leftovers) == 1  # 被拒绝的那次没留下清单
    finally:
        gate.set()
        app.close()


def test_real_run_without_key_is_refused(tmp_path: Path) -> None:
    app = App(tmp_path, token=TOKEN, popen=SpyPopen())
    app.set_mode({"mode": "real"})
    urls = _seed_real(tmp_path)
    with pytest.raises(ApiError) as info:
        app.translate_fulltext({"urls": [urls["wp_old"]], "dry_run": False})
    assert info.value.status == 400 and "key" in info.value.message
    est = app.translate_fulltext({"urls": [urls["wp_old"]], "dry_run": True})
    assert est["backend"] is None and est["count"] == 1  # 没 key 也能估，只是不报后端


def test_dry_run_uses_injected_backend_name_in_demo(tmp_path: Path) -> None:
    from qbkb.translate.fake import FakeTranslator

    app = App(
        tmp_path, token=TOKEN, popen=SpyPopen(), translator_factory=lambda _ws: FakeTranslator()
    )
    ws = workspace(tmp_path, "demo")
    ws.base.mkdir(parents=True)
    conn = store.connect(ws.db)
    url = _add(conn, "demo", "d1", section=None)
    conn.close()
    est = app.translate_fulltext({"urls": [url], "dry_run": True})
    assert est["count"] == 1 and est["backend"] == "fake" and est["quota_left"] is None
    # 演示模式的真跑：命令带 --translator demo 和演示配置，不带真实 key
    assert jobs.translate_urls_commands(ws, Path("x.txt"), python="py")[0][4:] == [
        "--db",
        str(ws.db),
        "--config-dir",
        str(ws.config_dir),
        "--site",
        "demo",
        "--translator",
        "demo",
        "--url-file",
        "x.txt",
    ]


def _tm_put(conn: Any, unit: str, glossary: Any, tgt: str, **kw: Any) -> None:
    store.tm_put(
        conn, segment_hash(unit), REAL_BACKEND, unit, tgt, glossary.version_for(unit), **kw
    )


def test_estimate_matches_pipeline_title_rejected_body_hit(tmp_path: Path) -> None:
    """标题只有 rejected 的翻译记忆 + 正文已命中：流水线仍会送标题，估算不能报 0。"""
    from qbkb.translate.fake import FakeTranslator
    from qbkb.translate.pipeline import title_unit, translate_article

    app = _real_app(tmp_path)
    conn = store.connect(tmp_path / "state.db")
    url = _add(conn, "cfm", "a", section="whitepaper", title_zh=None)
    glossary = app._glossary(app.ws())  # pyright: ignore[reportPrivateUsage]
    body = "<p>Some English paragraph for the article.</p>"
    _tm_put(conn, body, glossary, "<p>已翻正文。</p>")
    _tm_put(conn, title_unit("Title a"), glossary, "<b>坏的</b>", quality=store.TM_REJECTED)
    conn.close()
    est = app.translate_fulltext({"urls": [url], "dry_run": True})
    title_len = len(title_unit("Title a"))
    assert est["chars"] == est["chars_base"] == title_len > 0
    assert est["chars_upper_bound"] is False  # 没有待翻正文，标题不重试：确切值
    # 和流水线对照：真实送出的就是这个标题，字符数相同
    tr = FakeTranslator(name=REAL_BACKEND)
    conn = store.connect(tmp_path / "state.db")
    translate_article(conn, url, tr, glossary)
    conn.close()
    assert tr.calls == [[title_unit("Title a")]] and tr.chars_used == est["chars"]


def test_estimate_base_equals_real_send_and_skips_rejected_body(tmp_path: Path) -> None:
    from qbkb.translate.fake import FakeTranslator
    from qbkb.translate.pipeline import translate_article

    app = _real_app(tmp_path)
    conn = store.connect(tmp_path / "state.db")
    url = _add(conn, "cfm", "b", section="whitepaper", title_zh=None)
    other = _add(conn, "cfm", "c", section="whitepaper", title_zh=None, body="Second body.")
    glossary = app._glossary(app.ws())  # pyright: ignore[reportPrivateUsage]
    # 第二篇的正文有 rejected 记录：流水线不自动重翻，不送也不该估
    _tm_put(conn, "<p>Second body.</p>", glossary, "<b>坏</b>", quality=store.TM_REJECTED)
    conn.close()
    est: Any = app.translate_fulltext({"urls": [url, other], "dry_run": True})
    tr = FakeTranslator(name=REAL_BACKEND)
    conn = store.connect(tmp_path / "state.db")
    translate_article(conn, url, tr, glossary)
    translate_article(conn, other, tr, glossary)
    conn.close()
    assert est["chars_base"] == tr.chars_used  # 所有请求都通过校验时，基础估算正好等于实际
    assert est["chars"] > est["chars_base"] and est["chars_upper_bound"] is True


@pytest.mark.parametrize(
    "bad",
    [
        "https://cfm.example.test/a\nhttps://cfm.example.test/b",
        "https://cfm.example.test/a\r",
        "https://cfm.example.test/a\x00",
        "https://cfm.example.test/a\x85b",
        "https://cfm.example.test/a b",
        " https://cfm.example.test/a",
        "https://cfm.example.test/a ",
        "#https://cfm.example.test/a",
    ],
)
def test_urls_with_control_chars_or_outer_whitespace_are_rejected(tmp_path: Path, bad: str) -> None:
    popen = SpyPopen()
    app = _real_app(tmp_path, popen)
    conn = store.connect(tmp_path / "state.db")
    # 状态库里真有这样的异常主键且已发布：入参校验也得先拒绝，不能把它拆成清单里的多行
    store.upsert_discovered(conn, DiscoveredUrl(bad, "cfm", "x"))
    store.set_status(conn, bad, Status.PUBLISHED, title="T")
    conn.close()
    for dry in (True, False):
        with pytest.raises(ApiError) as info:
            app.translate_fulltext({"urls": [bad], "dry_run": dry})
        assert info.value.status == 400
    assert popen.calls == [] and not list((tmp_path / ".qbkb").glob("translate-urls-*.txt"))


def test_url_list_is_checked_line_by_line_before_use(tmp_path: Path) -> None:
    app = _real_app(tmp_path)
    ok = app._write_url_list(["https://x.test/a", "https://x.test/b"])  # pyright: ignore[reportPrivateUsage]
    assert ok.read_text(encoding="utf-8").splitlines() == ["https://x.test/a", "https://x.test/b"]
    with pytest.raises(ApiError) as info:
        app._write_url_list(["https://x.test/a\nhttps://x.test/b"])  # pyright: ignore[reportPrivateUsage]
    assert info.value.status == 400
    assert [p for p in (tmp_path / ".qbkb").glob("translate-urls-*.txt")] == [ok]  # 坏清单已删


# ---- 栏目翻译与其它翻译入口共用一把任务锁 --------------------------------------


class _Blocking:
    """假翻译后端：翻译时先报到，等放行才返回（模拟栏目翻译正在调付费接口）。"""

    name = REAL_BACKEND

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def translate(self, segments: Any, glossary: Any, context: str = "") -> list[str]:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5)
        return [str(s) for s in segments]


def _section_app(
    root: Path, popen: SpyPopen, tr: _Blocking, monkeypatch: pytest.MonkeyPatch
) -> tuple[App, str]:

    (root / ".env").write_text(f"DEEPL_API_KEY={DEEPL}\n", encoding="utf-8")
    app = App(
        root,
        token=TOKEN,
        popen=popen,
        environ={"PATH": "x"},
        is_windows=False,
        kill=lambda p: p.terminate(),
        translator_factory=lambda _ws: tr,  # pyright: ignore[reportArgumentType]
    )
    app.set_mode({"mode": "real"})
    conn = store.connect(root / "state.db")
    url = _add(conn, "cfm", "s1", section="whitepaper", title_zh=None)
    conn.close()
    csv = root / "survey.csv"
    csv.write_text("x", encoding="utf-8")
    monkeypatch.setattr(app, "_resolve_section_site", lambda ws, site: "cfm")
    monkeypatch.setattr(app, "_csv_path", lambda ws, site: csv)
    monkeypatch.setattr(server.survey, "read_csv", lambda p: [{"栏目id": "w", "网址": url}])
    monkeypatch.setattr(app, "sections_view", lambda site: {})
    return app, url


def test_section_translation_blocks_fulltext_and_run_until_it_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tr = _Blocking()
    popen = SpyPopen()
    app, url = _section_app(tmp_path, popen, tr, monkeypatch)
    errors: list[BaseException] = []

    def section() -> None:
        try:
            app.translate_section({"site": "cfm", "section": "w"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    t = threading.Thread(target=section)
    t.start()
    try:
        assert tr.entered.wait(5)
        for call in (
            lambda: app.translate_fulltext({"urls": [url], "dry_run": False}),
            lambda: app.start_run({"limit": 1}),
        ):
            with pytest.raises(ApiError) as info:
                call()
            assert info.value.status == 409
        with pytest.raises(ApiError) as info:  # 再来一个栏目翻译：仍是 409
            app.translate_section({"site": "cfm", "section": "w"})
        assert info.value.status == 409
        assert popen.calls == [] and not list((tmp_path / ".qbkb").glob("translate-urls-*.txt"))
        # 估算只读，不受锁影响
        assert app.translate_fulltext({"urls": [url], "dry_run": True})["count"] == 1
    finally:
        tr.release.set()
        t.join(5)
    assert not errors
    # 栏目翻译结束后锁释放：全文翻译可以开始
    assert app.translate_fulltext({"urls": [url], "dry_run": False}) == {
        "started": True,
        "count": 1,
    }
    app.close()


def test_running_job_blocks_section_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    tr = _Blocking()
    app, url = _section_app(tmp_path, SpyPopen(gate), tr, monkeypatch)
    try:
        assert app.translate_fulltext({"urls": [url], "dry_run": False})["started"]
        with pytest.raises(ApiError) as info:
            app.translate_section({"site": "cfm", "section": "w"})
        assert info.value.status == 409 and tr.calls == 0  # 一次付费调用都没发
        # 被拒绝后栏目的"正在翻译"标志已清掉，不会把以后的栏目翻译锁死
        assert app._translating is False  # pyright: ignore[reportPrivateUsage]
    finally:
        gate.set()
        app.close()
    tr.release.set()
    app.translate_section({"site": "cfm", "section": "w"})
    assert tr.calls >= 1  # 任务结束后恢复正常


def test_section_lock_excludes_other_processes_using_run_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定时任务（auto.py）用同一个 RunLock：栏目翻译持有期间它拿不到。"""
    tr = _Blocking()
    app, _ = _section_app(tmp_path, SpyPopen(), tr, monkeypatch)
    with app.jobs.exclusive("栏目翻译"):
        other = jobs.RunLock(tmp_path)
        with pytest.raises(jobs.Busy):
            other.acquire("定时任务")
    other.acquire("定时任务")  # 释放后可以拿
    other.release()


def test_translate_urls_commands_real(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "real")
    tr, ex = jobs.translate_urls_commands(ws, tmp_path / "u.txt", python="py")
    assert tr == [
        "py",
        "-m",
        "qbkb.cli",
        "translate",
        "--db",
        str(ws.db),
        "--url-file",
        str(tmp_path / "u.txt"),
    ]
    assert ex[3] == "export" and ex[ex.index("--out") + 1] == str(ws.out)
