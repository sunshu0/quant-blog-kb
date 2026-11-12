"""每天自动更新：只检查生成的 schtasks 参数和任务 XML，**不真的创建任务**（runner 是假的）。"""

import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest

from qbkb.console import schedule
from qbkb.console.schedule import MARKER, TASK_NAME, ScheduleError, Scheduler

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
ROOT = Path("D:/dev/R&D 项目/quant-blog-kb")
PY = Path("D:/dev/R&D 项目/quant-blog-kb/.venv/Scripts/pythonw.exe")


def _parse(xml: str) -> ET.Element:
    # ElementTree 不接受声明了 UTF-16 的 str，去掉声明再解析
    return ET.fromstring(xml.split("?>", 1)[1])


def test_task_xml_runs_daily_and_catches_up() -> None:
    xml = schedule.task_xml(ROOT, PY, "07:45")
    task = _parse(xml)
    trig = task.find("t:Triggers/t:CalendarTrigger", NS)
    assert trig is not None
    assert trig.findtext("t:StartBoundary", namespaces=NS) == "2026-01-01T07:45:00"
    assert trig.findtext("t:ScheduleByDay/t:DaysInterval", namespaces=NS) == "1"
    settings = task.find("t:Settings", NS)
    assert settings is not None
    assert settings.findtext("t:StartWhenAvailable", namespaces=NS) == "true"  # 错过后补跑
    assert settings.findtext("t:MultipleInstancesPolicy", namespaces=NS) == "IgnoreNew"
    assert settings.findtext("t:DisallowStartIfOnBatteries", namespaces=NS) == "false"
    assert settings.findtext("t:ExecutionTimeLimit", namespaces=NS) == "PT2H"
    exe = task.find("t:Actions/t:Exec", NS)
    assert exe is not None
    # 不依赖终端：用 .venv 的 pythonw（绝对路径），工作目录是项目目录
    assert exe.findtext("t:Command", namespaces=NS) == str(PY)
    assert exe.findtext("t:Arguments", namespaces=NS) == f'-m qbkb.console.auto --root "{ROOT}"'
    assert exe.findtext("t:WorkingDirectory", namespaces=NS) == str(ROOT)
    principal = task.find("t:Principals/t:Principal", NS)
    assert principal is not None
    assert principal.findtext("t:LogonType", namespaces=NS) == "InteractiveToken"  # 不存密码
    assert task.findtext("t:RegistrationInfo/t:Author", namespaces=NS) == MARKER
    assert MARKER in (task.findtext("t:RegistrationInfo/t:Description", namespaces=NS) or "")


@pytest.mark.parametrize("bad", ["9:00", "24:00", "09:60", "nine", ""])
def test_task_xml_rejects_bad_time(bad: str) -> None:
    with pytest.raises(ValueError):
        schedule.task_xml(ROOT, PY, bad)


def test_command_arguments() -> None:
    assert schedule.create_args(Path("C:/t/x.xml")) == [
        "schtasks",
        "/Create",
        "/TN",
        TASK_NAME,
        "/XML",
        str(Path("C:/t/x.xml")),
        "/F",
    ]
    assert schedule.delete_args() == ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"]
    assert schedule.query_args() == ["schtasks", "/Query", "/TN", TASK_NAME, "/XML"]
    assert TASK_NAME.startswith("qbkb")  # 任务名带项目名


class FakeRunner:
    """记录 schtasks 调用；/Query 按 existing 返回。/Create 时把 XML 文件读出来。"""

    def __init__(self, existing: str | None = None, fail: str | None = None) -> None:
        self.existing = existing
        self.fail = fail
        self.calls: list[list[str]] = []
        self.created_xml: bytes | None = None

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        verb = args[1]
        if verb == "/Query":
            if self.existing is None:
                return subprocess.CompletedProcess(args, 1, "", "错误: 系统找不到指定的文件。")
            return subprocess.CompletedProcess(args, 0, self.existing, "")
        if verb == "/Create":
            self.created_xml = Path(args[args.index("/XML") + 1]).read_bytes()
        code = 1 if self.fail == verb else 0
        return subprocess.CompletedProcess(args, code, "", "拒绝访问。" if code else "")


def test_enable_writes_utf16_xml_and_cleans_up(tmp_path: Path) -> None:
    runner = FakeRunner()
    Scheduler(runner, tmp_dir=tmp_path).enable(ROOT, "09:00", python=PY)
    assert [c[1] for c in runner.calls] == ["/Query", "/Create"]
    assert runner.created_xml is not None and runner.created_xml.startswith(b"\xff\xfe")  # BOM
    text = runner.created_xml.decode("utf-16")
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in text
    assert "R&amp;D" in text  # 路径里的 & 被转义
    assert list(tmp_path.iterdir()) == []  # 临时 XML 删掉了


def test_enable_replaces_our_own_task_but_not_others(tmp_path: Path) -> None:
    ours = FakeRunner(existing=f"<Task><Author>{MARKER}</Author></Task>")
    Scheduler(ours, tmp_dir=tmp_path).enable(ROOT, "10:00", python=PY)
    assert ours.calls[-1][1] == "/Create"
    theirs = FakeRunner(existing="<Task><Author>someone</Author></Task>")
    with pytest.raises(ScheduleError):
        Scheduler(theirs, tmp_dir=tmp_path).enable(ROOT, "10:00", python=PY)
    assert [c[1] for c in theirs.calls] == ["/Query"]


def test_disable_deletes_only_our_task(tmp_path: Path) -> None:
    ours = FakeRunner(existing=f"<Task><Author>{MARKER}</Author></Task>")
    assert Scheduler(ours).disable() is True
    assert ours.calls[-1] == schedule.delete_args()
    none = FakeRunner()
    assert Scheduler(none).disable() is False
    assert [c[1] for c in none.calls] == ["/Query"]
    theirs = FakeRunner(existing="<Task/>")
    with pytest.raises(ScheduleError):
        Scheduler(theirs).disable()
    assert all(c[1] != "/Delete" for c in theirs.calls)


def test_create_failure_is_reported(tmp_path: Path) -> None:
    with pytest.raises(ScheduleError) as info:
        Scheduler(FakeRunner(fail="/Create"), tmp_dir=tmp_path).enable(ROOT, "09:00", python=PY)
    assert "拒绝访问" in str(info.value)


def test_task_python_prefers_venv_pythonw(tmp_path: Path) -> None:
    exe = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert schedule.task_python(tmp_path) == exe
