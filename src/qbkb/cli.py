"""命令行入口。Phase 0 只有 init-db 和 sites；其余子命令在 Phase 2 串联时加入。"""

from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Sequence

from qbkb import config, store


def main(argv: Sequence[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文会乱码；统一输出 UTF-8。
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="qbkb", description="量化机构博客中文知识库")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="创建或升级状态库")
    p_init.add_argument("--db", default="state.db")

    sub.add_parser("sites", help="列出已配置站点及其合规结论")

    args = parser.parse_args(argv)
    if args.command == "init-db":
        store.connect(args.db).close()
        print(f"状态库就绪: {args.db}")
    elif args.command == "sites":
        for name in config.list_sites():
            site = config.load_site(name)
            print(f"{name}\t{site.compliance_decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
