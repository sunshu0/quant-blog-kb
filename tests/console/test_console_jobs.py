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
        self.stdout: Any = self

    def __iter__(self) -> Any:
        if self._gate is not None:
            self._gate.wait(5)
        return iter([line + "\n" for line in self._lines])

    def wait(self) -> int:
        return self._code

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
    assert args["--scorer"] == "fake" and args["--translator"] == "demo"
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


def test_run_lock_is_exclusive_and_recovers_stale(tmp_path: Path) -> None:
    a, b = jobs.RunLock(tmp_path), jobs.RunLock(tmp_path)
    a.acquire("控制台")
    with pytest.raises(jobs.Busy) as info:
        b.acquire("定时任务")
    assert "控制台" in str(info.value)
    a.release()
    b.acquire("定时任务")
    b.release()
    # 残留的锁（超过 STALE_SECONDS）会被接管
    stale = jobs.RunLock(tmp_path, clock=lambda: time.time() + jobs.STALE_SECONDS + 60)
    jobs.RunLock(tmp_path).acquire("崩掉的进程")
    stale.acquire("新任务")
    assert stale.holder() is not None and stale.holder()["who"] == "新任务"  # type: ignore[index]


def _manager(tmp_path: Path, popen: FakePopen, **kw: Any) -> jobs.JobManager:
    return jobs.JobManager(tmp_path, popen=popen, kill=lambda p: p.terminate(), **kw)


LINES = [
    "[discover] demo: 新 7、更新 0",
    "[translate] demo: 待翻译 4 篇",
    "[translate] 翻译后本月额度：已用 1200 / 上限 1000000 字符（剩余 998800）",
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
    assert not m.lock.path.exists()  # 跑完放锁
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


def test_failed_spawn_releases_lock(tmp_path: Path) -> None:
    def boom(cmd: list[str], **kw: Any) -> FakeProc:
        raise FileNotFoundError("no python")

    m = jobs.JobManager(tmp_path, popen=boom)
    job = m.start(workspace(tmp_path, "demo"), "run", "运行", [["a"]], {})
    m.wait(5)
    assert job.exit_code == jobs.EXIT_ERROR and "启动任务失败" in job.lines[-1]
    assert not m.lock.path.exists()


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
