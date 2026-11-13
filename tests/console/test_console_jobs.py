"""后台任务：命令构造、退出码说明、单任务锁、子进程输出收集（用假的子进程，不真的跑流程）。"""

import json
import os
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from qbkb.console import jobs
from qbkb.console.workspace import workspace

PY = "python"


class FakeProc:
    """假的子进程：stdout 输出给定的行，wait 返回给定的退出码；gate 不为空时输出前先等它。"""

    def __init__(self, lines: Sequence[str], code: int, gate: threading.Event | None = None):
        self._lines = list(lines)
        self._code = code
        self._gate = gate
        self.pid = 4242
        self.terminated = False
        self.finished = False
        self.stdout: Any = self

    def __iter__(self) -> Any:
        if self._gate is not None:
            self._gate.wait(5)
        return iter([line + "\n" for line in self._lines])

    def wait(self) -> int:
        self.finished = True
        return self._code

    def poll(self) -> int | None:
        return self._code if self.finished else None

    def terminate(self) -> None:
        self.terminated = True


class FakePopen:
    def __init__(self, *procs: FakeProc) -> None:
        self.procs = list(procs)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], **kwargs: Any) -> FakeProc:
        self.calls.append({"cmd": cmd, **kwargs})
        return self.procs.pop(0)


def test_run_command_demo_uses_isolated_paths_and_fake_backends(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "demo")
    cmd = jobs.run_command(ws, 3, python=PY)
    assert cmd[:4] == [PY, "-m", "qbkb.cli", "run"]
    args = dict(zip(cmd[4::2], cmd[5::2], strict=False))
    assert args["--db"] == str(ws.db) and str(ws.db).startswith(str(tmp_path / ".qbkb" / "demo"))
    assert args["--raw-dir"] == str(ws.raw) and args["--out"] == str(ws.out)
    assert args["--config-dir"] == str(ws.config_dir) and args["--site"] == "demo"
    assert "--scorer" not in cmd and args["--translator"] == "demo"  # v2：run 不打分
    assert args["--limit"] == "3" and args["--fetch-delay"] == "0"


def test_run_command_real_uses_real_backends(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "real")
    cmd = jobs.run_command(ws, 5, python=PY)
    assert "--scorer" not in cmd and "--translator" not in cmd and "--fetch-delay" not in cmd
    assert "--config-dir" not in cmd and "--site" not in cmd
    assert cmd[cmd.index("--db") + 1] == str(tmp_path / "state.db")


def test_retranslate_commands(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "real")
    tr, ex = jobs.retranslate_commands(ws, "https://x.test/a", "man", python=PY)
    assert tr[3] == "translate" and "--retry-rejected" in tr
    assert tr[tr.index("--url") + 1] == "https://x.test/a"
    assert tr[tr.index("--site") + 1] == "man"
    assert ex[3] == "export" and ex[ex.index("--out") + 1] == str(ws.out)
    demo_tr, _ = jobs.retranslate_commands(workspace(tmp_path, "demo"), None, "demo", python=PY)
    assert "--url" not in demo_tr and demo_tr[demo_tr.index("--translator") + 1] == "demo"


@pytest.mark.parametrize(
    ("codes", "final"),
    [
        ([0, 0], 0),
        ([5, 0], 5),
        ([3, 5], 3),
        ([5, 4, 3], 4),
        ([0, 1], 1),
        ([jobs.EXIT_STOPPED], jobs.EXIT_STOPPED),
        ([0, 7], 7),
    ],
)
def test_combine(codes: list[int], final: int) -> None:
    assert jobs.combine(codes) == final


def test_describe_exit_messages() -> None:
    assert "正常" in jobs.describe_exit(0)
    assert "额度用完" in jobs.describe_exit(3) and "接着翻" in jobs.describe_exit(3)
    assert "后端暂时不可用" in jobs.describe_exit(4)
    assert "待复查" in jobs.describe_exit(5)
    assert "未知" in jobs.describe_exit(42)
    assert jobs.describe_exit(None) == "还在运行。"


def test_execute_stops_after_error_and_passes_utf8_env(tmp_path: Path) -> None:
    popen = FakePopen(FakeProc(["a"], 1), FakeProc(["b"], 0))
    seen: list[str] = []
    code = jobs.execute(
        [["x"], ["y"]], cwd=tmp_path, env=jobs.child_env({}), on_line=seen.append, popen=popen
    )
    assert code == 1 and seen == ["a"] and len(popen.calls) == 1
    call = popen.calls[0]
    assert call["env"]["PYTHONUTF8"] == "1" and call["env"]["PYTHONIOENCODING"] == "utf-8"
    assert call["encoding"] == "utf-8" and call["cwd"] == str(tmp_path)


def test_execute_runs_export_after_translate_needs_attention(tmp_path: Path) -> None:
    popen = FakePopen(FakeProc([], 5), FakeProc([], 0))
    assert jobs.execute([["t"], ["e"]], cwd=tmp_path, env={}, on_line=print, popen=popen) == 5
    assert len(popen.calls) == 2


def test_run_lock_is_exclusive(tmp_path: Path) -> None:
    a, b = jobs.RunLock(tmp_path), jobs.RunLock(tmp_path)
    a.acquire("控制台")
    with pytest.raises(jobs.Busy) as info:
        b.acquire("定时任务")
    assert "控制台" in str(info.value)
    h = a.holder()
    assert h is not None and h["pid"] == os.getpid() and isinstance(h["started"], float)
    a.release()
    b.acquire("定时任务")
    b.release()
    # 锁文件不删除（互斥由操作系统持有），但元数据已清空
    assert b.path.exists() and b.holder() is None


def _write_lock(tmp_path: Path, data: dict[str, object]) -> Path:
    """模仿一个已持有的锁：第 0 字节是锁字节，第 1 字节起是元数据。"""
    path = tmp_path / ".qbkb" / jobs.LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = b"\x00" + json.dumps(data).encode("utf-8")
    path.write_bytes(payload)
    return path


def test_stale_metadata_file_does_not_block_acquire(tmp_path: Path) -> None:
    """旧进程留下的元数据（进程已死）不再需要残留判断：操作系统根本没持有锁，直接能拿到。"""
    _write_lock(tmp_path, {"who": "崩掉的控制台", "pid": 999999, "started": 1.0, "since": "x"})
    lock = jobs.RunLock(tmp_path)
    lock.acquire("新任务")
    assert lock.holder()["who"] == "新任务"  # type: ignore[index]


def test_metadata_write_failure_releases_os_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = jobs.RunLock(tmp_path)

    def boom(fd: int, data: dict[str, Any]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lock, "_write_info", boom)
    with pytest.raises(OSError):
        lock.acquire("控制台")
    assert not lock.held and lock._fd is None
    # 锁已经释放：另一个锁能立刻拿到，不会一直 Busy 到重启
    other = jobs.RunLock(tmp_path)
    other.acquire("定时任务")
    other.release()


def test_holder_reads_metadata_and_release_clears_it(tmp_path: Path) -> None:
    _write_lock(tmp_path, {"who": "旧任务", "pid": 1, "started": 1.0, "since": "x", "token": "t"})
    assert jobs.RunLock(tmp_path).holder()["who"] == "旧任务"  # type: ignore[index]
    lock = jobs.RunLock(tmp_path)
    lock.acquire("控制台")
    assert lock.holder()["who"] == "控制台"  # type: ignore[index]
    lock.release()
    assert lock.holder() is None  # 元数据清空，文件还在


def test_os_lock_auto_released_on_process_exit(tmp_path: Path) -> None:
    """进程退出（被硬杀）后操作系统自动放锁，另一个进程不用等就能拿到。"""
    import subprocess

    root = str(tmp_path)
    ready = tmp_path / "ready"
    code = (
        "import sys, time; "
        "from pathlib import Path; "
        "from qbkb.console import jobs; "
        "l = jobs.RunLock(Path(sys.argv[1])); "
        "l.acquire('child'); "
        "open(sys.argv[2], 'w').write('ok'); "
        "time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", code, root, str(ready)])
    try:
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.01)
        assert ready.exists()
        with pytest.raises(jobs.Busy):
            jobs.RunLock(tmp_path).acquire("parent")
    finally:
        child.kill()
        child.wait()
    lock = jobs.RunLock(tmp_path)
    # 子进程死了，操作系统自动放锁；teardown 可能比 wait() 返回晚一瞬，轮询等一下
    for _ in range(200):
        try:
            lock.acquire("parent")
            break
        except jobs.Busy:
            time.sleep(0.01)
    else:
        raise AssertionError("子进程退出后锁仍未释放")
    lock.release()


def test_touch_refreshes_lock_while_held(tmp_path: Path) -> None:
    lock = jobs.RunLock(tmp_path)
    lock.acquire("控制台")
    os.utime(lock.path, (1, 1))
    lock.touch()
    assert lock.path.stat().st_mtime > 1
    lock.release()
    lock.touch()  # 没持有时什么都不做
    assert lock.holder() is None


def _manager(tmp_path: Path, popen: FakePopen, **kw: Any) -> jobs.JobManager:
    return jobs.JobManager(tmp_path, popen=popen, kill=lambda p: p.terminate(), **kw)


LINES = [
    "[discover] demo: 新 7、更新 0",
    "[titles] demo: 补翻标题 4 / 4 篇（只翻标题，不翻正文）",
    "[summarize] demo: 新生成 4、复用 0、输入截取 0",
    "[titles] 翻译后本月额度：已用 1200 / 上限 1000000 字符（剩余 998800）",
    "[export] demo: 发布 3（内容没变 1）、退回重翻 0",
    "错误：key sk-abcdefghijklmnop9876 不对",
]


def test_job_manager_collects_output_and_result(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "demo")
    popen = FakePopen(FakeProc(LINES, 5))
    done: list[jobs.Job] = []
    m = _manager(
        tmp_path, popen, secrets=lambda: ["sk-abcdefghijklmnop9876"], on_finish=done.append
    )
    job = m.start(ws, "run", "运行", [["cmd"]], {"A": "1"})
    m.wait(5)
    assert job.exit_code == 5 and done == [job]
    s = job.summary()
    assert s["running"] is False and "待复查" in str(s["result"])
    assert s["stage"] == "export" and s["progress"] == {"done": 6, "total": 6}
    assert job.quota == {"used": 1200, "limit": 1000000, "remaining": 998800}
    assert job.published == 2
    assert "sk-abcdefghijklmnop9876" not in "\n".join(job.lines)  # key 被换成 ***
    assert job.log_path is not None
    log = Path(job.log_path).read_text("utf-8")
    assert "***" in log and "sk-abcdefghijklmnop9876" not in log
    assert Path(job.log_path).parent == ws.logs
    assert not m.lock.held and m.lock.holder() is None  # 跑完放锁
    assert popen.calls[0]["env"]["A"] == "1"
    # 增量取日志
    assert job.summary(since=3)["lines"] == job.lines[3:]


def test_only_one_job_at_a_time(tmp_path: Path) -> None:
    ws = workspace(tmp_path, "demo")
    gate = threading.Event()
    popen = FakePopen(FakeProc(["slow"], 0, gate), FakeProc([], 0))
    m = _manager(tmp_path, popen)
    m.start(ws, "run", "第一个", [["a"]], {})
    with pytest.raises(jobs.Busy):
        m.start(ws, "run", "第二个", [["b"]], {})
    # 另一个进程（定时任务）也拿不到锁
    with pytest.raises(jobs.Busy):
        jobs.RunLock(tmp_path).acquire("定时任务")
    gate.set()
    m.wait(5)
    second = m.start(ws, "run", "第二个", [["b"]], {})
    m.wait(5)
    assert second.exit_code == 0


def test_start_refused_while_scheduled_run_holds_lock(tmp_path: Path) -> None:
    other = jobs.RunLock(tmp_path)
    other.acquire("每天自动更新")
    m = _manager(tmp_path, FakePopen(FakeProc([], 0)))
    with pytest.raises(jobs.Busy) as info:
        m.start(workspace(tmp_path, "demo"), "run", "运行", [["a"]], {})
    assert "每天自动更新" in str(info.value)
    assert m.job is None


def test_stop_kills_child_and_reports_stopped(tmp_path: Path) -> None:
    gate = threading.Event()
    proc = FakeProc(["x"], 1, gate)
    m = _manager(tmp_path, FakePopen(proc, FakeProc([], 0)))
    job = m.start(workspace(tmp_path, "demo"), "run", "运行", [["a"], ["b"]], {})
    for _ in range(100):
        if m._proc is not None:  # pyright: ignore[reportPrivateUsage]
            break
        time.sleep(0.01)
    assert m.stop() is True
    gate.set()
    m.wait(5)
    assert proc.terminated and job.exit_code == jobs.EXIT_STOPPED
    assert "已停止" in str(job.summary()["result"])
    assert m.stop() is False


def test_stop_between_commands_kills_nothing(tmp_path: Path) -> None:
    """重翻是两条命令：第一条已经退出、第二条还没起的时候点停止，不能去杀第一条的旧 pid。"""
    killed: list[int] = []
    gate = threading.Event()
    first = FakeProc(["translate done"], 0)

    class Gated(FakePopen):
        def __call__(self, cmd: list[str], **kwargs: Any) -> FakeProc:
            if len(self.calls) == 1:
                gate.wait(5)  # 第二条命令启动前停一下，让测试在"两条之间"点停止
            return super().__call__(cmd, **kwargs)

    second = FakeProc(["export"], 0)
    m = jobs.JobManager(tmp_path, popen=Gated(first, second), kill=lambda p: killed.append(p.pid))
    job = m.start(workspace(tmp_path, "demo"), "retranslate", "重翻", [["t"], ["e"]], {})
    for _ in range(200):
        if first.finished and m._proc is None:  # pyright: ignore[reportPrivateUsage]
            break
        time.sleep(0.01)
    assert first.finished and m._proc is None  # pyright: ignore[reportPrivateUsage]
    assert m.stop() is True
    gate.set()
    m.wait(5)
    assert killed == []  # 旧 pid 没被杀
    assert job.exit_code == jobs.EXIT_STOPPED


def test_kill_tree_skips_finished_process() -> None:
    proc = FakeProc([], 0)
    proc.wait()
    jobs.kill_tree(proc)  # 已经退出：不调用 taskkill / terminate
    assert proc.terminated is False


def test_start_failure_after_lock_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = _manager(tmp_path, FakePopen(FakeProc([], 0)))
    ws = workspace(tmp_path, "demo")

    class NoThread:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def start(self) -> None:
            assert m.lock.path.exists()  # 这时候锁已经拿到了
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(jobs.threading, "Thread", NoThread)
    with pytest.raises(RuntimeError):
        m.start(ws, "run", "运行", [["a"]], {})
    monkeypatch.undo()
    assert not m.lock.held and m.job is None
    job = m.start(ws, "run", "运行", [["a"]], {})  # 之后还能正常开始
    m.wait(5)
    assert job.exit_code == 0


def test_child_env_bypasses_proxy_for_localhost() -> None:
    env = jobs.child_env({"HTTP_PROXY": "http://proxy.test:8080", "NO_PROXY": "corp.test"})
    assert env["NO_PROXY"] == "corp.test,127.0.0.1,localhost,::1"
    assert env["no_proxy"] == "127.0.0.1,localhost,::1"
    assert env["HTTP_PROXY"] == "http://proxy.test:8080"  # 真实站点照常走代理
    assert jobs.child_env(env)["NO_PROXY"] == env["NO_PROXY"]  # 重复调用不重复追加


def test_job_env_prefers_dotenv_keys_and_strips_them_in_demo(tmp_path: Path) -> None:
    environ = {"DEEPL_API_KEY": "old-from-shell", "PATH": "p"}
    keys = {"DEEPL_API_KEY": "new-from-dotenv", "LLM_API_KEY": "sk-x"}
    real = jobs.job_env(workspace(tmp_path, "real"), environ, keys)
    assert real["DEEPL_API_KEY"] == "new-from-dotenv" and real["LLM_API_KEY"] == "sk-x"
    assert "127.0.0.1" in real["NO_PROXY"]
    demo = jobs.job_env(workspace(tmp_path, "demo"), environ, keys)
    assert "DEEPL_API_KEY" not in demo and "LLM_API_KEY" not in demo
    assert "127.0.0.1" in demo["no_proxy"] and demo["QBKB_DEMO_STATE"]


def test_failed_spawn_releases_lock(tmp_path: Path) -> None:
    def boom(cmd: list[str], **kw: Any) -> FakeProc:
        raise FileNotFoundError("no python")

    m = jobs.JobManager(tmp_path, popen=boom)
    job = m.start(workspace(tmp_path, "demo"), "run", "运行", [["a"]], {})
    m.wait(5)
    assert job.exit_code == jobs.EXIT_ERROR and "启动任务失败" in job.lines[-1]
    assert not m.lock.held and m.lock.holder() is None


def test_real_subprocess_output_is_utf8(tmp_path: Path) -> None:
    """真的起一个子进程：中文输出经过管道不乱码（Windows 默认 GBK）。"""
    seen: list[str] = []
    code = jobs.execute(
        [[sys.executable, "-c", "print('[translate] 中文输出 ✓')"]],
        cwd=tmp_path,
        env=jobs.child_env(os.environ),
        on_line=seen.append,
    )
    assert code == 0 and seen == ["[translate] 中文输出 ✓"]


def test_job_summary_is_json_serialisable(tmp_path: Path) -> None:
    m = _manager(tmp_path, FakePopen(FakeProc(LINES, 0)))
    job = m.start(workspace(tmp_path, "demo"), "run", "运行", [["a"]], {})
    m.wait(5)
    json.dumps(job.summary(), ensure_ascii=False)


def test_full_translate_stage_name_and_progress(tmp_path: Path) -> None:
    """ds-1：--full-translate 时 translate 阶段有名字，进度按 full 阶段序列算（--skip 也去掉）。"""
    ws = workspace(tmp_path, "demo")
    lines = ["[discover] demo: 新 1", "[translate] demo: 完成 1 篇、需复查 0 篇"]
    popen = FakePopen(FakeProc(lines, 5))
    done: list[jobs.Job] = []
    m = _manager(tmp_path, popen, on_finish=done.append)
    cmd = [PY, "-m", "qbkb.cli", "run", "--full-translate", "--skip", "summarize"]
    job = m.start(ws, "run", "运行", [cmd], {})
    m.wait(5)
    s = job.summary()
    assert s["stage_name"] == "翻译全文"
    assert s["progress"] == {"done": 4, "total": 5}  # discover/fetch/extract/translate/export
