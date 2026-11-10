"""本地调试用：`uv run python -m qbkb.extract --site man FILE URL`，打印抽取结果 JSON。

正式流程的 CLI 子命令在 Phase 2 串联时加进 cli.py。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

from qbkb import config
from qbkb.extract.core import extract_full
from qbkb.extract.quality import check_quality
from qbkb.extract.rules import load_rules


def main(argv: list[str] | None = None) -> int:
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m qbkb.extract", description="抽取一个本地 HTML")
    parser.add_argument("--site", required=True)
    parser.add_argument("file", type=Path)
    parser.add_argument("url")
    args = parser.parse_args(argv)
    rules = load_rules(config.load_site(args.site))
    result = extract_full(args.file.read_bytes(), args.url, rules)
    meta = result.article.meta
    out = {
        "method": result.method,
        "meta": {
            "title": meta.title,
            "author": meta.author,
            "published": meta.published,
            "tags": list(meta.tags),
        },
        "n_body": result.n_body,
        "issues": check_quality(result, rules),
        "segments": [s.html for s in result.article.segments],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
