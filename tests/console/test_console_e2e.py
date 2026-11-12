"""演示模式端到端：通过 App 点「运行」（真的开子进程跑 qbkb run、子子进程跑 Scrapy，
抓本机示例站点），
再「重翻」待复查的文章；以及每天自动更新的入口。全部离线、不花额度，数据只在临时目录里。"""

from pathlib import Path

from qbkb.console import auto, jobs
from qbkb.console.server import App
from qbkb.console.workspace import Settings, read_json, save_settings, workspace


def _wait(app: App) -> jobs.Job:
    app.jobs.wait(180)
    job = app.jobs.job
    assert job is not None and job.exit_code is not None, "任务没在 180 秒内结束"
    return job


def test_demo_run_then_retranslate(tmp_path: Path) -> None:
    app = App(tmp_path, token="t", is_windows=False)
    try:
        assert app.mode() == "demo"
        app.start_run({"limit": 10})
        job = _wait(app)
        log = "\n".join(job.lines)
        # 有一篇需复查（演示用的"丢脚注"）、一篇 404 失败 → 退出码 5
        assert job.exit_code == jobs.EXIT_ATTENTION, log
        assert job.stage == "export" and job.published == 3
        toc = app.articles()
        assert toc["total"] == 3
        review = {i["status"]: i for i in app.review()["items"]}  # type: ignore[union-attr]
        assert set(review) == {"needs_review", "failed"}
        assert "404" in review["failed"]["reason"]

        first = toc["groups"][0]["articles"][0]  # type: ignore[index]
        art = app.article(first["url"], "zh")
        assert "（" in str(art["html"])  # 术语首次出现附英文
        trend = next(
            a
            for g in toc["groups"]  # type: ignore[union-attr]
            for a in g["articles"]
            if "trend" in a["url"]
        )
        html = str(app.article(trend["url"], "zh")["html"])
        assert "趋势跟踪（trend following）" in html and 'src="/out/demo/images/' in html
        name = html.split('src="/out/', 1)[1].split('"', 1)[0]
        assert app.out_file(name).read_bytes().startswith(b"\x89PNG")
        bi = str(app.article(trend["url"], "bi")["html"])
        assert "Trend following is one of the oldest ideas" in bi

        app.retranslate({"url": review["needs_review"]["url"]})
        job2 = _wait(app)
        assert job2.exit_code == jobs.EXIT_OK, "\n".join(job2.lines)
        assert app.articles()["total"] == 4
        assert [i["status"] for i in app.review()["items"]] == ["failed"]  # type: ignore[union-attr]
    finally:
        app.close()
    # 真实模式的数据目录一点没动
    for name in ("state.db", "raw", "output"):
        assert not (tmp_path / name).exists()
    assert workspace(tmp_path, "demo").db.is_file()


def test_auto_update_runs_and_records_result(tmp_path: Path) -> None:
    s = Settings()
    s.schedule.enabled, s.schedule.limit = True, 10
    save_settings(tmp_path, s)
    code = auto.run_auto(tmp_path)
    assert code == jobs.EXIT_ATTENTION
    last = read_json(tmp_path / ".qbkb" / auto.LAST_AUTO)
    assert last is not None and last["new"] == 3 and last["exit_code"] == 5
    assert last["mode"] == "demo" and "待复查" in last["result"]
    log = (workspace(tmp_path, "demo").logs / auto.LOG_NAME).read_text("utf-8")
    assert "自动更新开始" in log and "[export] demo: 发布 3" in log
    assert not jobs.RunLock(tmp_path).path.exists()


def test_auto_update_skips_when_disabled_or_busy(tmp_path: Path) -> None:
    assert auto.run_auto(tmp_path) == 0  # 默认关闭
    last = read_json(tmp_path / ".qbkb" / auto.LAST_AUTO)
    assert last is not None and "已关闭" in last["result"]
    s = Settings()
    s.schedule.enabled = True
    save_settings(tmp_path, s)
    lock = jobs.RunLock(tmp_path)
    lock.acquire("控制台：运行")
    try:
        assert auto.run_auto(tmp_path) == 0
    finally:
        lock.release()
    last = read_json(tmp_path / ".qbkb" / auto.LAST_AUTO)
    assert last is not None and "跳过" in last["result"] and "控制台" in last["result"]
    assert not workspace(tmp_path, "demo").db.exists()
