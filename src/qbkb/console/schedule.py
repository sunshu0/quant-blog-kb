"""每天自动更新（D9）：Windows 任务计划。

用任务的 XML 定义创建（`schtasks /Create /XML`），因为命令行参数设不了"错过后尽快补跑"
（StartWhenAvailable）和工作目录。任务执行的是 `.venv` 里的 pythonw.exe（没有黑窗口、
不依赖 uv 在不在 PATH 上）：`pythonw -m qbkb.console.auto --root <项目目录>`，
工作目录是项目目录，日志写在数据目录的 logs/auto-update.log。

只管我们自己建的任务：名字固定为 TASK_NAME，描述里带 MARKER；删除前先查询，描述里没有
MARKER 的同名任务不删。测试只检查生成的参数和 XML，不真的调用 schtasks。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

from qbkb.console.workspace import check_time

TASK_NAME = "qbkb-daily-update"
MARKER = "qbkb-console"
# 一次自动更新最多跑这么久（任务计划的执行时限）
TIME_LIMIT = "PT2H"

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def default_runner(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # schtasks 的输出是系统代码页（中文 Windows 是 GBK）；这里只看退出码和 XML 里的 ASCII 标记
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="mbcs" if os.name == "nt" else "utf-8",
        errors="replace",
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def task_python(root: Path) -> Path:
    """任务用的解释器：只用项目 .venv 里的 pythonw.exe。

    不回退到别的解释器：python.exe 每天会弹一个黑窗口，别处的解释器也可能没装 qbkb，
    那样任务每天都"运行"但什么都没做。找不到就报错，让用户先 uv sync。
    """
    venv = root / ".venv" / "Scripts" / "pythonw.exe"
    if not venv.is_file():
        raise ScheduleError(f"找不到 {venv}：请先在项目目录运行一次 uv sync，再打开每天自动更新")
    return venv


def task_xml(root: Path, python: Path, at: str) -> str:
    """任务的 XML 定义：每天 at 运行一次；错过了（关机、睡眠）开机后尽快补跑；
    只在用户登录时运行（不需要保存密码）；不管是否用电池；同一时间只跑一个实例。"""
    check_time(at)
    desc = f"{MARKER}：每天自动更新量化博客中文知识库（{root}）。在控制台的设置页可以关闭。"
    args = f'-m qbkb.console.auto --root "{root}"'
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{MARKER}</Author>
    <Description>{escape(desc)}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{at}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>{TIME_LIMIT}</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(python))}</Command>
      <Arguments>{escape(args)}</Arguments>
      <WorkingDirectory>{escape(str(root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def create_args(xml_path: Path) -> list[str]:
    return ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"]


def query_args() -> list[str]:
    return ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"]


def delete_args() -> list[str]:
    return ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]


class ScheduleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TaskState:
    exists: bool
    ours: bool


class Scheduler:
    """创建 / 查询 / 删除我们自己的任务。runner 可替换（测试里记录参数，不真的执行）。"""

    def __init__(self, runner: Runner = default_runner, tmp_dir: Path | None = None) -> None:
        self._run = runner
        self._tmp = tmp_dir

    def state(self) -> TaskState:
        r = self._run(query_args())
        if r.returncode != 0:
            return TaskState(False, False)
        return TaskState(True, MARKER in (r.stdout or ""))

    def enable(self, root: Path, at: str, python: Path | None = None) -> None:
        python = python or task_python(root)  # 先检查解释器，找不到就不去动任务计划
        st = self.state()
        if st.exists and not st.ours:
            raise ScheduleError(
                f"任务计划里已经有一个叫 {TASK_NAME} 的任务，但不是控制台建的，没有覆盖"
            )
        xml = task_xml(root, python, at)
        fd, name = tempfile.mkstemp(prefix="qbkb-task-", suffix=".xml", dir=self._tmp)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(xml.encode("utf-16"))  # 带 BOM 的 UTF-16，和 XML 声明一致
            r = self._run(create_args(path))
        finally:
            path.unlink(missing_ok=True)
        if r.returncode != 0:
            raise ScheduleError(f"创建任务计划失败：{(r.stderr or r.stdout or '').strip()}")

    def disable(self) -> bool:
        """删除我们的任务。不存在返回 False；同名但不是我们建的，报错不删。"""
        st = self.state()
        if not st.exists:
            return False
        if not st.ours:
            raise ScheduleError(f"任务 {TASK_NAME} 不是控制台建的，没有删除")
        r = self._run(delete_args())
        if r.returncode != 0:
            raise ScheduleError(f"删除任务计划失败：{(r.stderr or r.stdout or '').strip()}")
        return True
