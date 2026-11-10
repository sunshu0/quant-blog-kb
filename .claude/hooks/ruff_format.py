"""PostToolUse hook：Edit/Write 改了 .py 文件后自动 ruff format。

从 stdin 读 hook JSON，取 tool_input.file_path。任何异常都吞掉并退出 0，
格式化失败不应打断 AI 的工作；真正的拦截由 scripts/verify.sh 负责。
"""

from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    try:
        # 中文 Windows 上 sys.stdin 默认按 GBK 解码，payload 里的中文路径/内容会解析失败；
        # hook JSON 是 UTF-8，直接读字节再解码。
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        path = payload.get("tool_input", {}).get("file_path", "")
        if isinstance(path, str) and path.endswith(".py"):
            subprocess.run(["ruff", "format", "--quiet", path], check=False, timeout=60)
    except Exception:  # noqa: BLE001 - hook 不能失败
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
