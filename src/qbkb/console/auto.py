"""每天自动更新的入口（任务计划执行）：`pythonw -m qbkb.console.auto --root <项目目录>`。

没有终端（pythonw 下 sys.stdout 是 None），所以什么都不打印，全部写进日志：
数据目录的 logs/auto-update.log（每次追加），结果记在 .qbkb/last_auto.json，
控制台的设置页显示"上次自动更新：时间、结果、新增几篇"。

- 设置里关掉了自动更新（任务还没删掉的那一会儿）：记一笔"跳过"就退出；
- 控制台正在跑任务：跳过这次（锁文件），不和它抢同一个状态库；
- 用的是当前模式：演示模式就在本进程里起示例站点再跑。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from qbkb import store
from qbkb.console import demo, jobs
from qbkb.console.workspace import (
    Workspace,
    load_settings,
    read_keys,
    redact,
    resolve_mode,
    state_dir,
    workspace,
    write_json,
)
from qbkb.models import Status

LAST_AUTO = "last_auto.json"
LOG_NAME = "auto-update.log"


def published_count(ws: Workspace) -> int:
    if not ws.db.is_file():
        return 0
    conn = store.connect(ws.db)
    try:
        return store.count_by_status(conn).get(Status.PUBLISHED, 0)
    finally:
        conn.close()


def record(root: Path, **fields: object) -> None:
    write_json(state_dir(root) / LAST_AUTO, {"time": _now(), **fields})


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_auto(root: Path, *, popen: jobs.Popen | None = None) -> int:
    settings = load_settings(root)
    keys = read_keys(root)
    mode = resolve_mode(settings.mode, keys)
    ws = workspace(root, mode)
    ws.logs.mkdir(parents=True, exist_ok=True)
    secrets = [v for v in keys.values() if v]
    with open(ws.logs / LOG_NAME, "a", encoding="utf-8", newline="\n") as log:

        def write(line: str) -> None:
            log.write(redact(line, secrets) + "\n")
            log.flush()

        write(f"==== {_now()} 自动更新开始（{'演示' if ws.demo else '真实'}模式）")
        if not settings.schedule.enabled:
            write("设置里已经关闭自动更新，跳过")
            record(root, mode=mode, exit_code=None, result="跳过：自动更新已关闭", new=0)
            return 0
        lock = jobs.RunLock(root)
        try:
            lock.acquire("每天自动更新")
        except jobs.Busy as e:
            write(f"跳过：{e}")
            record(root, mode=mode, exit_code=None, result=f"跳过：{e}", new=0)
            return 0
        site: demo.DemoSite | None = None
        try:
            env = dict(os.environ)
            if ws.demo:
                site = demo.DemoSite(ws)
                site.start()
                env = demo.demo_env(ws, env)
            before = published_count(ws)
            cmd = jobs.run_command(ws, settings.schedule.limit, python=_console_python())
            code = jobs.execute(
                [cmd],
                cwd=root,
                env=jobs.child_env(env),
                on_line=write,
                popen=popen or subprocess.Popen,
            )
            new = max(published_count(ws) - before, 0)
        except Exception as e:  # 定时任务没人看着：一定要留下记录
            write(f"错误：{type(e).__name__}: {e}")
            code, new = jobs.EXIT_ERROR, 0
        finally:
            lock.release()
            if site is not None:
                site.stop()
        result = jobs.describe_exit(code)
        write(f"==== {_now()} 结束：退出码 {code}，{result} 新增 {new} 篇")
        record(root, mode=mode, exit_code=code, result=result, new=new)
        return code


def _console_python() -> str:
    """子进程用 python.exe（不是 pythonw）：它的输出要通过管道读回来。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        p = exe.with_name("python.exe")
        if p.is_file():
            return str(p)
    return str(exe)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m qbkb.console.auto")
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_auto(args.root.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
