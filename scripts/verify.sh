#!/usr/bin/env bash
# 唯一的完成标准：退出码 0。离线运行，不访问网站、不花 API 额度。
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff check"
uv run ruff check .
echo "== ruff format --check"
uv run ruff format --check .
echo "== pyright"
uv run pyright
echo "== pytest"
uv run pytest -q -m "not network"
echo "verify: OK"
