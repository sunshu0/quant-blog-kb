"""演示模式：示例站点、演示翻译后端、演示配置、数据隔离。全部离线。"""

import socket
from pathlib import Path

import httpx

from qbkb.config import load_site
from qbkb.console import demo
from qbkb.console.workspace import workspace
from qbkb.translate import same_structure


def test_demo_translator_uses_bundled_chinese_and_keeps_structure() -> None:
    t = demo.DemoTranslator()
    first = demo.PAGES[0].blocks[0]
    src = f'<p class="x">{first.en}</p>'
    (out,) = t.translate([src], {})
    assert out == f'<p class="x">{first.zh}</p>'
    assert same_structure(src, out)
    # 不认识的段落：只在文字前加前缀，标签不动
    (other,) = t.translate(["<p>Unknown <em>words</em>.</p>"], {})
    assert other == "<p>【演示译文】Unknown <em>words</em>.</p>"
    # 只有图片的段落原样返回
    img = '<p><img src="http://127.0.0.1:1/files/x.png" alt="a"></p>'
    assert t.translate([img], {}) == [img]
    assert t.name == "demo" and t.chars_used > 0


def test_demo_translator_titles() -> None:
    t = demo.DemoTranslator()
    assert t.translate(["<p>Trend Following Basics</p>"], {}) == ["<p>趋势跟踪基础</p>"]


def test_flaky_paragraph_drops_footnote_twice_then_recovers(tmp_path: Path) -> None:
    t = demo.DemoTranslator(tmp_path)
    src = f"<p>{demo.PAGES[1].blocks[0].en}</p>"
    outs = [t.translate([src], {})[0] for _ in range(3)]
    assert [same_structure(src, o) for o in outs] == [False, False, True]
    # 没有状态目录（测试、命令行直接用）时从不出错
    assert same_structure(src, demo.DemoTranslator().translate([src], {})[0])


def test_every_translated_block_has_chinese_and_matching_sup() -> None:
    for page in demo.PAGES:
        for b in page.blocks:
            if b.zh and b.tag != "img":
                assert b.en.count("<sup>") == b.zh.count("<sup>"), b.en[:30]


def test_demo_content_is_self_written() -> None:
    """示例文章都声明自己是构造的演示内容，不冒充真实文章。"""
    kept = [p for p in demo.PAGES if p.title_zh]
    assert len(kept) == 4 and len(demo.PAGES) == 6
    for p in kept:
        text = " ".join(b.en for b in p.blocks).lower()
        assert any(w in text for w in ("constructed", "made-up", "invented", "made up")), p.path


def test_chart_is_a_png() -> None:
    png = demo.chart_png()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and png.endswith(b"IEND\xaeB`\x82")


def test_site_serves_pages_and_404_and_keeps_its_port(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "demo")
    site = demo.DemoSite(ws)
    base = site.start()
    try:
        assert base.startswith("http://127.0.0.1:")
        assert site.start() == base  # 重复调用不再起
        r = httpx.get(base + "/sitemap.xml", trust_env=False)
        assert r.status_code == 200 and demo.MISSING_PATH in r.text
        assert httpx.get(base + demo.PAGES[0].path, trust_env=False).status_code == 200
        assert httpx.get(base + demo.MISSING_PATH, trust_env=False).status_code == 404
        assert (
            httpx.get(base + demo.CHART_PATH, trust_env=False).headers["Content-Type"]
            == "image/png"
        )
    finally:
        site.stop()
    # 下次启动用同一个端口（文章 URL 里带端口）
    again = demo.DemoSite(ws)
    try:
        assert again.start() == base
    finally:
        again.stop()


def test_busy_saved_port_resets_demo_data(tmp_path: Path) -> None:
    """保存的端口被别的程序（不是示例站点）占了：清空演示数据，换端口。"""
    ws = workspace(tmp_path, "demo")
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    try:
        port = blocker.getsockname()[1]
        ws.base.mkdir(parents=True)
        (ws.base / demo.SITE_FILE).write_text(f'{{"port": {port}}}', "utf-8")
        ws.db.write_bytes(b"old")
        (ws.out / "demo").mkdir(parents=True)
        site = demo.DemoSite(ws)
        try:
            base = site.start()
            assert base != f"http://127.0.0.1:{port}"
            assert not ws.db.exists() and not ws.out.exists()  # 旧 URL 对不上了，清掉
        finally:
            site.stop()
    finally:
        blocker.close()


def test_second_process_reuses_running_demo_site(tmp_path: Path) -> None:
    """控制台开着（示例站点在跑）时，每天的定时任务也起示例站点：直接用已有的，不清数据。"""
    ws = workspace(tmp_path, "demo")
    console = demo.DemoSite(ws)
    base = console.start()
    try:
        ws.db.write_bytes(b"keep")
        scheduled = demo.DemoSite(ws)
        assert scheduled.start() == base
        assert ws.db.read_bytes() == b"keep"
        assert demo.is_demo_site(int(base.rsplit(":", 1)[1]))
        scheduled.stop()  # 没起自己的服务，stop 不影响控制台的
        assert httpx.get(base + "/robots.txt", trust_env=False).status_code == 200
    finally:
        console.stop()


def test_ensure_config_points_to_local_site_only(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "demo")
    cfg = demo.ensure_config(ws, "http://127.0.0.1:12345")
    assert sorted(p.stem for p in (cfg / "sites").glob("*.yaml")) == ["demo"]
    site = load_site("demo", cfg)
    assert site.compliance_decision == "allowed"
    assert site.raw["base_url"] == "http://127.0.0.1:12345"
    assert site.raw["discover"]["sitemap"] == "http://127.0.0.1:12345/sitemap.xml"
    # 栏目照抄 man，演示时只采集独立文章
    collect = {s["id"]: s["collect"] for s in site.raw["sections"]["list"]}
    assert {k for k, v in collect.items() if v} == {"article"}
    assert not (cfg / "fixed_translations.yaml").exists()  # 本机的固定译文不带进演示配置
    assert (cfg / "glossary.yaml").is_file() and list((cfg / "rubrics").glob("*.md"))


def test_reset_only_touches_demo(tmp_path: Path) -> None:
    real = workspace(tmp_path, "real")
    real.db.write_bytes(b"real")
    (real.out / "man").mkdir(parents=True)
    ws = workspace(tmp_path, "demo")
    ws.base.mkdir(parents=True)
    ws.db.write_bytes(b"demo")
    (ws.raw / "demo").mkdir(parents=True)
    demo.reset_data(ws)
    assert not ws.db.exists() and not ws.raw.exists()
    assert real.db.read_bytes() == b"real" and (real.out / "man").is_dir()
    try:
        demo.reset_data(real)
    except ValueError:
        pass
    else:
        raise AssertionError("真实模式的数据不能被清空")


def test_demo_env_drops_keys() -> None:
    ws = workspace(Path("/x"), "demo")
    env = demo.demo_env(ws, {"DEEPL_API_KEY": "k", "LLM_API_KEY": "k", "PATH": "p"})
    assert env == {"PATH": "p", demo.DEMO_STATE_ENV: str(ws.base)}
