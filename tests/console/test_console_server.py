"""控制台服务：接口的安全检查（Host、token、Origin、Content-Type）、key 的保存和掩码、
测试连接（假的 HTTP）、定时任务开关（假的任务计划）、文章和图片接口。"""

import json
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from qbkb import store
from qbkb.console import server
from qbkb.console.schedule import MARKER, Scheduler
from qbkb.console.server import ApiError, App
from qbkb.console.workspace import load_settings, read_keys, workspace
from qbkb.models import DiscoveredUrl, Status

DEEPL = "0123abcd-1111-2222-3333-444455556666:fx"
LLM = "sk-abcdefghijklmnop9876"
TOKEN = "test-token-123"


class FakeTasks:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.exists = False

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if args[1] == "/Query":
            out = f"<Task><Author>{MARKER}</Author></Task>" if self.exists else ""
            return subprocess.CompletedProcess(args, 0 if self.exists else 1, out, "")
        self.exists = args[1] == "/Create"
        return subprocess.CompletedProcess(args, 0, "", "")


def _app(root: Path, transport: httpx.BaseTransport | None = None) -> tuple[App, FakeTasks]:
    tasks = FakeTasks()
    app = App(
        root,
        token=TOKEN,
        scheduler=Scheduler(tasks, tmp_dir=root),
        transport=transport,
        environ={"PATH": "x"},
        is_windows=True,
    )
    return app, tasks


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[App, httpx.Client]]:
    app, _ = _app(tmp_path)
    srv = server.make_server(app, 0)
    import threading

    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5)
    try:
        yield app, client
    finally:
        client.close()
        srv.shutdown()
        srv.server_close()


def _post(client: httpx.Client, path: str, body: object, **headers: str) -> httpx.Response:
    h = {"Content-Type": "application/json", "X-QBKB-Token": TOKEN, **headers}
    return client.post(path, content=json.dumps(body), headers=h)


def test_server_binds_localhost_only(served: tuple[App, httpx.Client]) -> None:
    _, client = served
    assert str(client.base_url).startswith("http://127.0.0.1:")


def test_index_has_token_and_security_headers(served: tuple[App, httpx.Client]) -> None:
    _, client = served
    r = client.get("/")
    assert r.status_code == 200
    assert f'name="qbkb-token" content="{TOKEN}"' in r.text
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    # 页面不从任何外部地址加载东西
    assert "http://" not in r.text and "https://" not in r.text
    assert client.get("/static/app.js").status_code == 200


def test_static_assets_load_nothing_from_cdn() -> None:
    for name in ("index.html", "app.js", "app.css"):
        text = (server.STATIC_DIR / name).read_text("utf-8")
        assert "//cdn" not in text and "googleapis" not in text and "unpkg" not in text
        assert "@import" not in text


def test_wrong_host_is_rejected(served: tuple[App, httpx.Client]) -> None:
    _, client = served
    r = client.get("/api/state", headers={"Host": "evil.test"})
    assert r.status_code == 403


def test_post_needs_token_json_and_same_origin(served: tuple[App, httpx.Client]) -> None:
    _, client = served
    body = {"mode": "demo"}
    assert _post(client, "/api/mode", body, **{"X-QBKB-Token": "wrong"}).status_code == 403
    no_token = client.post(
        "/api/mode", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert no_token.status_code == 403
    form = client.post(
        "/api/mode",
        content="mode=demo",
        headers={"Content-Type": "text/plain", "X-QBKB-Token": TOKEN},
    )
    assert form.status_code == 415  # 简单请求（表单 / text/plain）不接受
    assert _post(client, "/api/mode", body, Origin="https://evil.test").status_code == 403
    port = str(client.base_url).rsplit(":", 1)[1]
    ok = _post(client, "/api/mode", body, Origin=f"http://127.0.0.1:{port}")
    assert ok.status_code == 200 and ok.json()["mode_setting"] == "demo"


def test_keys_are_saved_to_env_and_never_returned(served: tuple[App, httpx.Client]) -> None:
    app, client = served
    r = _post(client, "/api/keys", {"deepl": DEEPL, "llm": LLM})
    assert r.status_code == 200
    assert DEEPL not in r.text and LLM not in r.text
    assert r.json()["keys"]["deepl"] == {"configured": True, "last4": "6:fx"}
    assert read_keys(app.root) == {"DEEPL_API_KEY": DEEPL, "LLM_API_KEY": LLM}
    state = client.get("/api/state")
    assert DEEPL not in state.text and LLM not in state.text
    assert state.json()["mode"] == "real"  # 两个 key 都有：自动切到真实模式
    bad = _post(client, "/api/keys", {"deepl": "abc\nEVIL=1"})
    assert bad.status_code == 400 and "EVIL" not in (app.root / ".env").read_text("utf-8")
    cleared = _post(client, "/api/keys", {"clear": ["deepl"]})
    assert cleared.json()["keys"]["deepl"]["configured"] is False


def _transport(seen: list[httpx.Request], status: int, body: object) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_deepl_check_uses_free_usage_endpoint(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    app, _ = _app(
        tmp_path, _transport(seen, 200, {"character_count": 1200, "character_limit": 10**6})
    )
    r = app.test_key({"which": "deepl", "key": DEEPL})
    assert r["ok"] is True and "1,200" in str(r["message"]) and "998,800" in str(r["message"])
    assert str(seen[0].url) == "https://api-free.deepl.com/v2/usage"
    assert seen[0].method == "GET"
    assert seen[0].headers["Authorization"] == f"DeepL-Auth-Key {DEEPL}"
    assert DEEPL not in json.dumps(r, ensure_ascii=False)
    assert app.state()["quota"]["remaining"] == 998800  # type: ignore[index]


def test_deepl_check_bad_key(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    app, _ = _app(tmp_path, _transport(seen, 403, {"message": "Forbidden"}))
    r = app.test_key({"which": "deepl", "key": "pro-key-without-suffix"})
    assert r["ok"] is False and "无效" in str(r["message"])
    assert str(seen[0].url) == "https://api.deepl.com/v2/usage"  # 不是 :fx 结尾：Pro 端点


def test_llm_check_lists_models(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    body = {"data": [{"id": "deepseek-flash"}, {"id": "deepseek-reasoner"}]}
    app, _ = _app(tmp_path, _transport(seen, 200, body))
    (tmp_path / ".env").write_text(f"LLM_API_KEY={LLM}\n", "utf-8")
    r = app.test_key({"which": "llm"})  # 不传 key：用已保存的
    assert r["ok"] is True and "deepseek-flash" in str(r["message"])
    assert str(seen[0].url) == "https://api.deepseek.com/models"
    assert seen[0].headers["Authorization"] == f"Bearer {LLM}"


def test_key_check_network_error_does_not_leak_key(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom {req.url}")

    app, _ = _app(tmp_path, httpx.MockTransport(handler))
    r = app.test_key({"which": "deepl", "key": DEEPL})
    assert r["ok"] is False and DEEPL not in str(r)
    assert app.test_key({"which": "llm"}) == {"ok": False, "message": "还没有填写这个 key"}


def test_schedule_toggle_creates_and_deletes_our_task(tmp_path: Path) -> None:
    app, tasks = _app(tmp_path)
    exe = tmp_path / ".venv" / "Scripts" / "pythonw.exe"  # uv sync 之后才有
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    st = app.set_schedule({"enabled": True, "time": "08:15", "limit": 3})
    assert st["schedule"] == {"enabled": True, "time": "08:15", "limit": 3, "supported": True}
    assert st["task"] == {"exists": True, "ours": True}
    assert any(c[1] == "/Create" for c in tasks.calls)
    s = load_settings(tmp_path)
    assert s.schedule.enabled and s.schedule.time == "08:15" and s.schedule.limit == 3
    st = app.set_schedule({"enabled": False})
    assert st["schedule"]["enabled"] is False  # type: ignore[index]
    assert ["/Delete" in c for c in tasks.calls].count(True) == 1 and not tasks.exists
    with pytest.raises(ApiError):
        app.set_schedule({"enabled": True, "time": "8am"})


def test_schedule_not_supported_off_windows(tmp_path: Path) -> None:
    app = App(tmp_path, token=TOKEN, is_windows=False)
    with pytest.raises(ApiError) as info:
        app.set_schedule({"enabled": True})
    assert "Windows" in info.value.message
    assert app.state()["task"] is None


def test_real_mode_without_keys_refuses_to_run(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    app.set_mode({"mode": "real"})
    with pytest.raises(ApiError) as info:
        app.start_run({"limit": 5})
    assert "key" in info.value.message
    with pytest.raises(ApiError):
        app.start_run({"limit": 0})


def _published(root: Path, tmp_out: Path) -> str:
    """在演示数据目录里放一篇已发布的文章（不跑流程），返回 URL。"""
    ws = workspace(root, "demo")
    ws.base.mkdir(parents=True, exist_ok=True)
    url = "http://127.0.0.1:1/insights/x"
    conn = store.connect(ws.db)
    store.upsert_discovered(conn, DiscoveredUrl(url, "demo", "x"))
    store.set_status(
        conn,
        url,
        Status.PUBLISHED,
        title="X",
        title_zh="标题<script>",
        published="2024-01-02",
        category="基础概念",
        value_score=8.0,
        translated_at="2026-11-12T00:00:00+00:00",
    )
    conn.close()
    from qbkb.export import article_paths

    main, bi = article_paths(ws.out, "demo", "2024-01-02", "x")
    main.parent.mkdir(parents=True)
    bi.parent.mkdir(parents=True)
    main.write_text(
        "---\ntitle: X\n---\n\n# 标题\n\n<script>alert(1)</script>\n\n![图](images/a.png)\n",
        "utf-8",
    )
    bi.write_text("---\ntitle: X\n---\n\nEnglish\n\n中文\n", "utf-8")
    (ws.out / "demo" / "images").mkdir()
    (ws.out / "demo" / "images" / "a.png").write_bytes(b"\x89PNG")
    (ws.base / "secret.txt").write_text("no", "utf-8")
    return url


def test_articles_and_article_and_images(served: tuple[App, httpx.Client], tmp_path: Path) -> None:
    app, client = served
    url = _published(app.root, tmp_path)
    toc = client.get("/api/articles").json()
    assert toc["total"] == 1 and toc["groups"][0]["category"] == "基础概念"
    assert toc["groups"][0]["articles"][0]["title"] == "标题<script>"  # 页面用 textContent 显示
    a = client.get("/api/article", params={"url": url, "view": "zh"}).json()
    assert "<script" not in a["html"] and 'src="/out/demo/images/a.png"' in a["html"]
    assert a["score"] == 8.0 and a["category"] == "基础概念" and a["source_url"] == url
    bi = client.get("/api/article", params={"url": url, "view": "bi"}).json()
    assert "English" in bi["html"]
    img = client.get("/out/demo/images/a.png")
    assert img.status_code == 200 and img.headers["Content-Type"] == "image/png"
    for bad in ("/out/../secret.txt", "/out/..%2Fsecret.txt", "/out/demo/x.md", "/out/../state.db"):
        assert client.get(bad).status_code == 404, bad
    assert client.get("/api/article", params={"url": "nope", "view": "zh"}).status_code == 404
    assert client.get("/api/article", params={"url": url, "view": "x"}).status_code == 400


def test_review_lists_needs_review_and_failed(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    ws = workspace(tmp_path, "demo")
    ws.base.mkdir(parents=True)
    conn = store.connect(ws.db)
    for i, (st, err) in enumerate(
        [(Status.NEEDS_REVIEW, "结构校验未通过的段落 0"), (Status.FAILED, "HTTP 404：页面不存在")]
    ):
        u = f"http://127.0.0.1:1/{i}"
        store.upsert_discovered(conn, DiscoveredUrl(u, "demo", str(i)))
        store.set_status(conn, u, st, error=err)
    conn.close()
    items = app.review()["items"]
    assert isinstance(items, list) and len(items) == 2
    by: dict[str, Any] = {i["status"]: i for i in items}
    assert by["needs_review"]["can_retranslate"] is True
    assert by["failed"]["can_retranslate"] is False and "404" in by["failed"]["reason"]
    with pytest.raises(ApiError):
        app.retranslate({"url": "http://127.0.0.1:1/1"})  # 失败的文章不能"重翻"


def test_real_mode_does_not_see_demo_articles(tmp_path: Path) -> None:
    app, _ = _app(tmp_path)
    _published(tmp_path, tmp_path)
    assert app.articles()["total"] == 1
    app.set_mode({"mode": "real"})
    assert app.articles()["total"] == 0
    assert not (tmp_path / "state.db").exists()  # 真实模式的状态库没被创建
