"""真跑 Scrapy 的集成测试：对本机 HTTP 服务（127.0.0.1）抓取，不访问外网。

每个场景一个子进程（Twisted reactor 不能在同一进程里重启）。
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HELPER = Path(__file__).with_name("crawl_local_helper.py")


def _run(scenario: str, work: Path) -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, str(HELPER), scenario, str(work)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-3000:]
    return json.loads((work / "result.json").read_text(encoding="utf-8"))


def test_backoff_pauses_the_whole_site(tmp_path: Path) -> None:
    result = _run("backoff", tmp_path)
    hits = result["hits"]
    article_hits = [h for h in hits if h["path"] != "/robots.txt"]
    failures = [i for i, h in enumerate(article_hits) if h["status"] == 503]
    assert len(failures) == 2
    # 第 n 次 503 之后，下一个请求（不管是哪个 URL）至少等 0.5 * 2**(n-1) 秒
    for n, i in enumerate(failures, start=1):
        wait = 0.5 * 2 ** (n - 1)
        after = article_hits[i + 1 :]
        assert after, "503 之后应该还有请求（重试或下一篇）"
        gap = after[0]["t"] - article_hits[i]["t"]
        assert gap >= wait - 0.05, (n, gap, [h["path"] for h in article_hits])
    assert {h["path"] for h in article_hits} == {"/insights/busy", "/insights/a", "/insights/b"}
    assert all(row["status"] == "fetched" for row in result["rows"].values())


def test_long_retry_after_stops_the_crawl(tmp_path: Path) -> None:
    result = _run("retry_after", tmp_path)
    hits = [h for h in result["hits"] if h["path"] != "/robots.txt"]
    slow = [i for i, h in enumerate(hits) if h["path"] == "/insights/slow"]
    assert len(slow) == 1  # 没有提前重试
    assert hits[slow[0] + 1 :] == []  # 之后整个站点不再请求
    rows = result["rows"]
    assert rows["slow"]["status"] == "failed"
    assert "Retry-After 3600" in rows["slow"]["error"]
    requested = {h["path"].rsplit("/", 1)[1] for h in hits}
    for slug in ("a", "b", "c"):
        expected = "fetched" if slug in requested else "discovered"
        assert rows[slug]["status"] == expected
