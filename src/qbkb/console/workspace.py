"""控制台的工作区：两种模式的数据目录、控制台设置文件、.env 里的 API key。

- 真实模式的数据就是项目根目录下的 `state.db`、`raw/`、`output/`、`logs/`（和命令行一样）；
- 演示模式的数据全部在 `.qbkb/demo/` 下，和真实数据完全隔离；
- 控制台自己的设置和记录在 `.qbkb/` 下（console.json、last_auto.json……），已被 .gitignore 忽略。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values

Mode = Literal["demo", "real"]
ModeSetting = Literal["auto", "demo", "real"]

# 默认项目根目录：src/qbkb/console/workspace.py 往上三级（uv sync 的可编辑安装）
DEFAULT_ROOT = Path(__file__).resolve().parents[3]
STATE_DIR = ".qbkb"
DEMO_DIR = "demo"
SETTINGS_FILE = "console.json"
DEMO_SITE = "demo"

DEEPL_KEY = "DEEPL_API_KEY"
LLM_KEY = "LLM_API_KEY"
KEY_NAMES = (DEEPL_KEY, LLM_KEY)
# key 只允许这些字符：DeepL 是 "uuid:fx"，DeepSeek 是 "sk-…"。拒绝换行、空格、引号、#，
# 防止往 .env 里注入别的变量
_KEY_VALUE = re.compile(r"^[A-Za-z0-9:._\-]{8,200}$")

DEFAULT_TIME = "09:00"
DEFAULT_LIMIT = 5
MAX_LIMIT = 50
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True, slots=True)
class Workspace:
    """一种模式的数据目录。命令行参数都用绝对路径，不依赖子进程的当前目录。"""

    mode: Mode
    root: Path  # 项目根目录（.env、config/ 所在）
    base: Path  # 数据目录：真实模式 = root；演示模式 = root/.qbkb/demo

    @property
    def demo(self) -> bool:
        return self.mode == "demo"

    @property
    def db(self) -> Path:
        return self.base / "state.db"

    @property
    def raw(self) -> Path:
        return self.base / "raw"

    @property
    def out(self) -> Path:
        return self.base / "output"

    @property
    def logs(self) -> Path:
        return self.base / "logs"

    @property
    def config_dir(self) -> Path | None:
        """演示模式用自己生成的配置（demo.ensure_config）；真实模式用仓库里的 config/。"""
        return self.base / "config" if self.demo else None

    @property
    def site(self) -> str | None:
        return DEMO_SITE if self.demo else None


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def workspace(root: Path, mode: Mode) -> Workspace:
    base = state_dir(root) / DEMO_DIR if mode == "demo" else root
    return Workspace(mode=mode, root=root, base=base)


# ---- 控制台设置 ---------------------------------------------------------------


@dataclass(slots=True)
class Schedule:
    enabled: bool = False
    time: str = DEFAULT_TIME
    limit: int = DEFAULT_LIMIT


@dataclass(slots=True)
class Settings:
    mode: ModeSetting = "auto"
    schedule: Schedule = field(default_factory=Schedule)


def check_time(value: object) -> str:
    if not isinstance(value, str) or not _TIME.match(value):
        raise ValueError("时间格式要是 HH:MM（例如 09:00）")
    return value


def check_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise ValueError(f"篇数要是 1 到 {MAX_LIMIT} 之间的整数")
    return value


def load_settings(root: Path) -> Settings:
    """读 .qbkb/console.json；没有或格式不对的项用默认值。"""
    path = state_dir(root) / SETTINGS_FILE
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    s = Settings()
    if data.get("mode") in ("auto", "demo", "real"):
        s.mode = data["mode"]
    sched = data.get("schedule")
    if isinstance(sched, dict):
        s.schedule.enabled = sched.get("enabled") is True
        with contextlib.suppress(ValueError):
            s.schedule.time = check_time(sched.get("time"))
        with contextlib.suppress(ValueError):
            s.schedule.limit = check_limit(sched.get("limit"))
    return s


def write_json(path: Path, data: object) -> None:
    """原子写入（先写临时文件再替换），UTF-8、LF。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    os.replace(tmp, path)


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_settings(root: Path, s: Settings) -> None:
    write_json(
        state_dir(root) / SETTINGS_FILE,
        {
            "mode": s.mode,
            "schedule": {
                "enabled": s.schedule.enabled,
                "time": s.schedule.time,
                "limit": s.schedule.limit,
            },
        },
    )


# ---- .env 里的 key -----------------------------------------------------------


def env_path(root: Path) -> Path:
    return root / ".env"


def read_keys(root: Path) -> dict[str, str]:
    """.env 里两个 key 的值（没配置的是空字符串）。只读文件，不改进程的环境变量。"""
    path = env_path(root)
    values = dotenv_values(path) if path.is_file() else {}
    return {name: (values.get(name) or "").strip() for name in KEY_NAMES}


def mask(value: str) -> dict[str, object]:
    """页面上显示用：只给"是否已配置"和后 4 位，从不返回完整 key。"""
    if not value:
        return {"configured": False, "last4": ""}
    return {"configured": True, "last4": value[-4:] if len(value) >= 8 else ""}


def check_key_value(value: str) -> str:
    value = value.strip()
    if not _KEY_VALUE.match(value):
        raise ValueError("key 格式不对：只能包含字母、数字和 : . _ -，长度 8–200")
    return value


def set_key(root: Path, name: str, value: str) -> None:
    """把一个 key 写进 .env（替换已有的那一行，没有就追加）；value 为空表示删掉这一行。

    其他行原样保留。写完后文件只有 LF 换行。
    """
    if name not in KEY_NAMES:
        raise ValueError(f"不认识的 key：{name}")
    if value:
        value = check_key_value(value)
    path = env_path(root)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pattern = re.compile(rf"^\s*(?:export\s+)?{re.escape(name)}\s*=")
    out: list[str] = []
    done = False
    for line in lines:
        if pattern.match(line):
            if value and not done:
                out.append(f"{name}={value}")
            done = True
            continue
        out.append(line)
    if value and not done:
        out.append(f"{name}={value}")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(("\n".join(out) + "\n").encode("utf-8") if out else b"")
    os.replace(tmp, path)


# 默认流程（v2）只需要翻译的 key：LLM 打分不在采集流程里了，LLM key 只给可选的加工步骤用
REQUIRED_KEYS = (DEEPL_KEY,)


def keys_configured(keys: dict[str, str]) -> bool:
    return all(keys.get(n) for n in REQUIRED_KEYS)


def resolve_mode(setting: ModeSetting, keys: dict[str, str]) -> Mode:
    """auto：翻译的 key 配置了就是真实模式，否则演示模式。"""
    if setting == "auto":
        return "real" if keys_configured(keys) else "demo"
    return setting


def redact(text: str, secrets: list[str]) -> str:
    """日志里万一出现 key，换成 ***（纵深防御：命令行本身不打印 key）。"""
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, "***")
    return text
