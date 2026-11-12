"""真实通道（本机 127.0.0.1 HTTP 服务）：整站停止机制（robots 跨站、Retry-After 太久）。

run_fetch 会启动 Twisted reactor，一个进程只能跑一次，所以每个场景在独立子进程里跑
（crawl_stop_channel.py），测试只断言它写回的 result.json。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("scenario", ["robots", "robots_retry", "retry_after", "queue"])
def test_fetch_stop_channel(scenario: str, tmp_path: Path) -> None:
    script = Path(__file__).with_name("crawl_stop_channel.py")
    result = subprocess.run(
        [sys.executable, str(script), scenario, str(tmp_path)],
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")[-3000:]
    data = json.loads((tmp_path / "result.json").read_text("utf-8"))
    if scenario == "robots":
        # robots.txt 302 到不同 hostname：没有任何文章被请求，也没被误标失败
        assert data["article_hits"] == []
        assert data["rows"] == {"a": "discovered", "b": "discovered", "c": "discovered"}
        assert "robots.txt 被重定向到未许可站点" in data["stop_reason"]
    elif scenario == "robots_retry":
        # robots.txt 长 Retry-After：fast 停站，任何文章都不被请求、不被误标失败
        assert data["article_hits"] == []
        assert data["rows"] == {"a": "discovered", "b": "discovered", "c": "discovered"}
        assert "Retry-After 3600" in data["stop_reason"]
        assert data["failed"] == 0 and data["fetched"] == 0
    else:
        # 第一篇 429 + Retry-After 3600（retry_after 单并发 / queue 并发 3）：
        # 只有 /a 被请求；/a 记失败，/b /c 不被请求、保持 discovered。
        assert data["article_hits"] == ["/a"]
        assert data["rows"]["a"] == "failed"
        assert data["rows"]["b"] == "discovered"
        assert data["rows"]["c"] == "discovered"
        assert "Retry-After 3600" in data["stop_reason"]
