"""后台任务：在子进程里跑 `qbkb run` / `qbkb translate`，实时收集输出，按退出码给出说明。

- Twisted reactor 一个进程只能启动一次，所以控制台从不在自己的进程里跑流程，一律开子进程；
- 同一时间只允许一个任务：进程内用 threading.Lock，跨进程（控制台和每天的定时任务）用
  数据目录下的锁文件 `.qbkb/run.lock`（操作系统持有的独占锁，进程退出自动释放；
  文件第 1 字节起写持有者元数据，只给人看）；
- 日志每个任务一份，写在数据目录的 logs/ 下；写之前把 key 换成 ***（纵深防御）。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any, Protocol

from qbkb.console import procinfo
from qbkb.console.workspace import Workspace, redact
from qbkb.console.workspace import state_dir as _state_dir

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_QUOTA = 3
EXIT_UNAVAILABLE = 4
EXIT_ATTENTION = 5
EXIT_STOPPED = -1  # 用户点了"停止"

EXIT_MESSAGES: dict[int, str] = {
    EXIT_OK: "正常完成。",
    EXIT_ERROR: "出错了：看日志里「错误：」开头的那一行（常见原因：没配置 key、网络不通）。",
    2: "参数错误（控制台自己的问题，请把日志发给维护的人）。",
    EXIT_QUOTA: (
        "额度用完：进度已经保存，额度恢复后（DeepL 是下个月）再点「运行」会从中断处接着翻。"
    ),
    EXIT_UNAVAILABLE: "后端暂时不可用（限流或网络问题）：进度已经保存，过一会儿再点「运行」。",
    EXIT_ATTENTION: "跑完了，但有文章需要处理：看「待复查」页（失败的原因、需要重翻的文章）。",
    EXIT_STOPPED: "已停止：进度已经保存，下次运行会接着跑。",
}
# 和 cli.STAGES 一致（v2 默认流程：分层翻译，只翻标题 + 摘要，不翻全文）
STAGES = ("discover", "fetch", "extract", "titles", "summarize", "export")
STAGE_NAMES = {
    "discover": "发现新文章",
    "fetch": "抓取",
    "extract": "抽取正文、判定栏目",
    "titles": "翻译标题",
    "summarize": "生成中文摘要",
    "export": "导出 Markdown",
}
_STAGE_LINE = re.compile(r"^\[(discover|fetch|extract|titles|translate|summarize|export)\]")
_QUOTA_LINE = re.compile(r"本月额度：已用 (\d+) / 上限 (\d+) 字符（剩余 (\d+)）")
_PUBLISHED_LINE = re.compile(r"^\[export\] \S+: 发布 (\d+)（内容没变 (\d+)）")
MAX_LINES = 5000
# 运行中每隔这么久刷新一次锁文件的修改时间
TOUCH_EVERY_S = 30
LOCK_FILE = "run.lock"
LOCAL_NO_PROXY = ("127.0.0.1", "localhost", "::1")


def describe_exit(code: int | None) -> str:
    if code is None:
        return "还在运行。"
    return EXIT_MESSAGES.get(code, f"子进程退出码 {code}（未知），看日志。")


def combine(codes: Iterable[int]) -> int:
    """几个命令的退出码合成一个。

    出错 > 停止 > 不可用 > 额度 > 需处理 > 正常（后四个和 cli 的顺序一致）。
    """
    seen = set(codes)
    for c in (EXIT_ERROR, EXIT_STOPPED, EXIT_UNAVAILABLE, EXIT_QUOTA, EXIT_ATTENTION):
        if c in seen:
            return c
    other = sorted(c for c in seen if c != EXIT_OK)
    return other[0] if other else EXIT_OK


# ---- 命令 -------------------------------------------------------------------


def base_args(ws: Workspace) -> list[str]:
    """各阶段共用的参数：数据路径一律给绝对路径；演示模式加上自己的配置目录和站点。"""
    args = ["--db", str(ws.db)]
    if ws.demo:
        assert ws.config_dir is not None and ws.site is not None
        args += ["--config-dir", str(ws.config_dir), "--site", ws.site]
    return args


def backend_args(ws: Workspace) -> list[str]:
    if ws.demo:
        return ["--translator", "demo"]
    return []


def run_command(ws: Workspace, limit: int, python: str = sys.executable) -> list[str]:
    """点「运行」：qbkb run，每个阶段最多处理 limit 篇。

    真实模式默认只翻标题 + 摘要，不翻全文（分层翻译）；演示模式要展示"翻译成中文知识库"，
    所以翻全文（DemoTranslator 自带中文），但没有 LLM key，跳过摘要。
    """
    cmd = [python, "-m", "qbkb.cli", "run", *base_args(ws), "--limit", str(limit)]
    cmd += ["--raw-dir", str(ws.raw), "--out", str(ws.out), *backend_args(ws)]
    if ws.demo:
        cmd += ["--fetch-delay", "0", "--full-translate", "--skip", "summarize"]
    return cmd


def retranslate_commands(
    ws: Workspace, url: str | None, site: str | None, python: str = sys.executable
) -> list[list[str]]:
    """「重翻」：translate --retry-rejected（限定这篇或这个站点），然后 export 把它发布出来。"""
    tr = [python, "-m", "qbkb.cli", "translate", *base_args(ws), "--retry-rejected"]
    if ws.demo:
        tr += ["--translator", "demo"]  # translate 只认 --translator
    if url:
        tr += ["--url", url]
    if site and not ws.demo:
        tr += ["--site", site]
    ex = [python, "-m", "qbkb.cli", "export", *base_args(ws), "--out", str(ws.out)]
    return [tr, ex]


def child_env(base: Mapping[str, str]) -> dict[str, str]:
    """子进程统一 UTF-8 输出（Windows 默认 GBK），不缓冲（实时看到进度）。"""
    env = dict(base)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    # 访问本机的请求不走代理：配了系统代理（HTTP_PROXY 等）的电脑上，Scrapy 和 httpx
    # 会把 127.0.0.1 的示例站点也交给代理，演示全部失败。真实站点照常走代理
    for name in ("NO_PROXY", "no_proxy"):
        have = [x.strip() for x in env.get(name, "").split(",") if x.strip()]
        env[name] = ",".join(have + [h for h in LOCAL_NO_PROXY if h not in have])
    return env


def job_env(ws: Workspace, environ: Mapping[str, str], keys: Mapping[str, str]) -> dict[str, str]:
    """任务子进程的环境变量（控制台和每天的定时任务用同一套规则）。

    - 演示模式：去掉 API key，带上演示状态目录；
    - 真实模式：以 .env 里的 key 为准（覆盖进程环境里可能过期的同名变量；子进程里的
      load_dotenv 不会覆盖已有变量）。
    """
    from qbkb.console import demo

    if ws.demo:
        return child_env(demo.demo_env(ws, environ))
    env = dict(environ)
    env.update({k: v for k, v in keys.items() if v})
    return child_env(env)


# ---- 跨进程锁 ----------------------------------------------------------------


class Busy(RuntimeError):
    """已经有任务在跑。"""


def _lock_nb(fd: int) -> None:
    """尝试对 fd 加独占锁（不阻塞）。拿不到抛 OSError。

    Windows 上 msvcrt.locking 锁文件第 0 字节（强制锁：别的进程读不到这个字节，但读第 1 字节
    起的元数据不受影响）；其他系统用 fcntl.flock（建议锁，锁整个文件）。
    """
    if os.name == "nt":
        import msvcrt

        # 锁字节必须存在：新文件先补 1 字节，元数据从第 1 字节起写
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\x00")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class RunLock:
    """一个数据根目录同时只跑一个任务（控制台和定时任务共用）。

    互斥由操作系统持有的独占锁保证：进程退出（哪怕被硬杀）时自动释放，不存在"判残留 →
    删除 → 重建"的竞态，也不用删除文件。锁文件第 1 字节起写持有者元数据（who / pid /
    started / since / token），只给人看：Busy 提示和 release 核验持有者用它，不影响互斥本身。
    持有期间 touch() 刷新修改时间，方便人看（`last_seen`）。
    """

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], float] = time.time,
        alive: Callable[[int, float | None], bool] = procinfo.same_process,
        me: Callable[[], tuple[int, float | None]] = lambda: (
            os.getpid(),
            procinfo.self_start_time(),
        ),
    ) -> None:
        self.path = _state_dir(root) / LOCK_FILE
        # clock / alive 已不再参与互斥（OS 锁取代了残留判断），保留参数只为兼容旧调用方
        self._me = me
        self._fd: int | None = None
        self._token: str | None = None
        self.held = False

    def holder(self) -> dict[str, Any] | None:
        """读持有者元数据（第 1 字节起）。读不到 / 不是 JSON 映射返回 None。"""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except OSError:
            return None
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            chunks: list[bytes] = []
            while True:
                block = os.read(fd, 4096)
                if not block:
                    break
                chunks.append(block)
        except OSError:
            return None
        finally:
            os.close(fd)
        try:
            data = json.loads(b"".join(chunks).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write_info(self, fd: int, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, 1 + len(payload))  # 清掉上一次更长元数据留下的尾巴

    def acquire(self, who: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR)
        try:
            _lock_nb(fd)
        except OSError as e:
            os.close(fd)
            h = self.holder() or {}
            raise Busy(
                f"已经有任务在运行（{h.get('who', '未知')}，{h.get('since', '?')} 开始）"
            ) from e
        try:
            pid, started = self._me()
            token = f"{pid}-{time.time_ns()}"
            self._write_info(
                fd,
                {
                    "who": who,
                    "pid": pid,
                    "started": started,
                    "since": datetime.now().isoformat(timespec="seconds"),
                    "token": token,
                },
            )
        except BaseException:
            # 取锁成功但写元数据失败：必须释放锁并关句柄，否则内核锁仍被本进程持有，
            # 之后所有新任务一直 Busy，直到重启控制台 / 进程
            with contextlib.suppress(OSError):
                _unlock(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        self._fd = fd
        self._token = token
        self.held = True

    def touch(self) -> None:
        if self.held:
            with contextlib.suppress(OSError):
                os.utime(self.path)

    def release(self) -> None:
        if not self.held or self._fd is None:
            return
        # 核验持有者：元数据 token 对得上才清空（OS 锁已经保证互斥，这里是纵深防御）。
        # 对不上（不该发生）就跳过清空，仍解锁关闭，把互斥交给操作系统。
        try:
            cur = self.holder()
            if cur is None or cur.get("token") == self._token:
                os.ftruncate(self._fd, 1)
        except OSError:
            pass
        with contextlib.suppress(OSError):
            _unlock(self._fd)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None
        self._token = None
        self.held = False


# ---- 执行 -------------------------------------------------------------------


class Proc(Protocol):
    stdout: IO[str] | None
    pid: int

    def wait(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


Popen = Callable[..., Proc]


def _no_window() -> int:
    # 控制台用 pythonw / 定时任务启动时，不要给每个子进程弹一个黑窗口
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(slots=True)
class Job:
    id: int
    kind: str  # run / retranslate
    title: str
    mode: str
    commands: list[list[str]]
    started: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished: str | None = None
    exit_code: int | None = None
    stage: str | None = None
    lines: list[str] = field(default_factory=list[str])
    dropped: int = 0  # 超过 MAX_LINES 后丢掉的最早几行
    quota: dict[str, int] | None = None
    published: int = 0  # export 新发布（内容有变化）的篇数
    log_path: str | None = None
    stop_requested: bool = False

    def add(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            del self.lines[0]
            self.dropped += 1
        m = _STAGE_LINE.match(line)
        if m:
            self.stage = m.group(1)
        q = _QUOTA_LINE.search(line)
        if q:
            used, limit, left = (int(x) for x in q.groups())
            self.quota = {"used": used, "limit": limit, "remaining": left}
        pub = _PUBLISHED_LINE.match(line)
        if pub:
            self.published += int(pub.group(1)) - int(pub.group(2))

    def summary(self, since: int = 0) -> dict[str, object]:
        """给页面的 JSON。since 是页面已经拿到的行数（含丢掉的），只返回之后的新行。"""
        start = max(since - self.dropped, 0)
        stage_idx = STAGES.index(self.stage) + 1 if self.stage in STAGES else 0
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "mode": self.mode,
            "started": self.started,
            "finished": self.finished,
            "running": self.exit_code is None,
            "exit_code": self.exit_code,
            "result": describe_exit(self.exit_code),
            "stage": self.stage,
            "stage_name": STAGE_NAMES.get(self.stage or "", ""),
            "progress": {"done": stage_idx, "total": len(STAGES)} if self.kind == "run" else None,
            "quota": self.quota,
            "published": self.published,
            "lines": self.lines[start:],
            "next": self.dropped + len(self.lines),
        }


def execute(
    commands: Sequence[Sequence[str]],
    *,
    cwd: Path,
    env: Mapping[str, str],
    on_line: Callable[[str], None],
    popen: Popen = subprocess.Popen,
    on_start: Callable[[Proc], None] | None = None,
    on_exit: Callable[[Proc], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """依次跑几个命令，每行输出交给 on_line。前一个出错（1、2）或被停止就不跑后面的。"""
    codes: list[int] = []
    for cmd in commands:
        if should_stop():
            codes.append(EXIT_STOPPED)
            break
        proc = popen(
            list(cmd),
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_no_window(),
        )
        if on_start is not None:
            on_start(proc)
        if proc.stdout is not None:
            for raw in proc.stdout:
                on_line(raw.rstrip("\r\n"))
        code = proc.wait()
        if on_exit is not None:
            on_exit(proc)  # 立刻忘掉这个 pid：退出后它可能被别的进程复用
        if should_stop():
            codes.append(EXIT_STOPPED)
            break
        codes.append(code)
        if code in (EXIT_ERROR, 2):
            break
    return combine(codes)


def kill_tree(proc: Proc) -> None:
    """结束子进程和它的子进程（run 会再开一个跑 Scrapy 的子进程）。Windows 上用 taskkill /T。

    只杀我们自己启动、而且还在运行的进程：先用 Popen 对象的 poll() 确认没退出
    （Popen 持有进程句柄，进程没退出时这个 pid 不会被复用）。已经退出的直接返回。
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        with contextlib.suppress(OSError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                creationflags=_no_window(),
            )
            return
    with contextlib.suppress(OSError):
        proc.terminate()


class JobManager:
    """控制台里的任务：同一时间只有一个，在后台线程里跑，页面轮询 summary。"""

    def __init__(
        self,
        root: Path,
        *,
        popen: Popen = subprocess.Popen,
        secrets: Callable[[], list[str]] = lambda: [],
        on_finish: Callable[[Job], None] | None = None,
        kill: Callable[[Proc], None] = kill_tree,
    ) -> None:
        self.root = root
        self._kill = kill
        self.lock = RunLock(root)
        self._popen = popen
        self._secrets = secrets
        self._on_finish = on_finish
        self._mutex = threading.Lock()
        self._job: Job | None = None
        self._proc: Proc | None = None
        self._proc_mutex = threading.Lock()
        self._thread: threading.Thread | None = None
        self._next_id = 1

    @property
    def job(self) -> Job | None:
        return self._job

    def running(self) -> bool:
        j = self._job
        return j is not None and j.exit_code is None

    def start(
        self,
        ws: Workspace,
        kind: str,
        title: str,
        commands: list[list[str]],
        env: Mapping[str, str],
    ) -> Job:
        with self._mutex:
            if self.running():
                raise Busy("已经有任务在运行，等它跑完再开始新的")
            self.lock.acquire(f"控制台：{title}")
            try:
                job = Job(self._next_id, kind, title, ws.mode, commands)
                ws.logs.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                job.log_path = str(ws.logs / f"console-{stamp}-{job.id}.log")
                thread = threading.Thread(
                    target=self._work, args=(job, ws, child_env(env)), daemon=True
                )
                thread.start()
            except BaseException:
                # 拿到锁之后出错（建不了日志目录、起不了线程）：把锁放掉，不然要等它被判成残留
                self.lock.release()
                raise
            self._next_id += 1
            self._job = job
            self._thread = thread
            return job

    def _work(self, job: Job, ws: Workspace, env: dict[str, str]) -> None:
        secrets = self._secrets()
        log: IO[str] | None = None
        try:
            assert job.log_path is not None
            log = open(job.log_path, "a", encoding="utf-8", newline="\n")  # noqa: SIM115

            last_touch = [time.monotonic()]

            def on_line(line: str) -> None:
                now = time.monotonic()
                if now - last_touch[0] >= TOUCH_EVERY_S:
                    self.lock.touch()
                    last_touch[0] = now
                line = redact(line, secrets)
                job.add(line)
                if log is not None:
                    log.write(line + "\n")
                    log.flush()

            def on_start(proc: Proc) -> None:
                with self._proc_mutex:
                    self._proc = proc

            def on_exit(proc: Proc) -> None:
                with self._proc_mutex:
                    if self._proc is proc:
                        self._proc = None

            code = execute(
                job.commands,
                cwd=ws.root,
                env=env,
                on_line=on_line,
                popen=self._popen,
                on_start=on_start,
                on_exit=on_exit,
                should_stop=lambda: job.stop_requested,
            )
        except Exception as e:  # 子进程都起不来（找不到 python 之类）：也要把锁放掉
            job.add(f"错误：控制台启动任务失败：{type(e).__name__}: {redact(str(e), secrets)}")
            code = EXIT_ERROR
        finally:
            if log is not None:
                log.close()
            with self._proc_mutex:
                self._proc = None
            self.lock.release()
        job.finished = datetime.now().isoformat(timespec="seconds")
        job.exit_code = code
        if self._on_finish is not None:
            with contextlib.suppress(Exception):
                self._on_finish(job)

    def stop(self) -> bool:
        """请求停止当前任务（结束子进程）。进度已经在状态库里，下次运行接着跑。"""
        job = self._job
        if job is None or job.exit_code is not None:
            return False
        job.stop_requested = True
        with self._proc_mutex:
            # 在锁里取出并杀：两条命令之间（上一个已退出、下一个还没起）这里是 None，什么都不杀；
            # 下一条命令看到 stop_requested 就不会再启动
            proc = self._proc
            if proc is not None:
                self._kill(proc)
        return True

    def wait(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)
