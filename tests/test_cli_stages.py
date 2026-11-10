"""CLI：status、eval、退出码、缺 key 的提示、fetch 子进程参数。端到端流程见 tests/e2e。"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from qbkb import cli, store
from qbkb.filter import ScorerUnavailableError
from qbkb.models import DiscoveredUrl, Score, Segment, Status, body_hash

URL = "https://www.man.com/insights/what-is-volatility"


def _db(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = store.connect(db)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "v"))
    store.upsert_discovered(conn, DiscoveredUrl(URL + "-2", "man", "v2"))
    store.upsert_discovered(conn, DiscoveredUrl(URL + "-3", "man", "v3"))
    store.set_status(conn, URL + "-2", Status.NEEDS_REVIEW, error="结构校验未通过的段落 3")
    store.set_status(conn, URL + "-3", Status.FAILED, error="HTTP 404：页面不存在")
    conn.close()
    return db


def test_status_counts_and_lists(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _db(tmp_path)
    assert cli.main(["status", "--db", str(db), "--site", "man"]) == 0
    out = capsys.readouterr().out
    assert "共 3 篇（站点 man）" in out
    assert "discovered        1" in out and "needs_review      1" in out
    assert "需要人工复查（needs_review，1 篇）" in out and "结构校验未通过的段落 3" in out
    assert "失败（failed，1 篇）" in out and "HTTP 404" in out


def _extracted(tmp_path: Path) -> Path:
    db = tmp_path / "state.db"
    conn = store.connect(db)
    store.upsert_discovered(conn, DiscoveredUrl(URL, "man", "v"))
    segs = [Segment(0, "<p>" + "word " * 400 + "</p>")]
    store.replace_segments(conn, URL, segs)
    store.set_status(
        conn, URL, Status.EXTRACTED, title="What is volatility", body_hash=body_hash(segs)
    )
    conn.close()
    return db


def test_missing_llm_key_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _extracted(tmp_path)
    monkeypatch.chdir(tmp_path)  # 没有 .env
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert cli.main(["filter", "--site", "man", "--db", str(db)]) == cli.EXIT_ERROR
    assert "LLM_API_KEY" in capsys.readouterr().err


def test_missing_deepl_key_only_when_there_is_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _extracted(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    args = ["translate", "--site", "man", "--db", str(db)]
    assert cli.main(args) == 0  # 没有 filtered 文章，用不到 key
    assert "没有待翻译的文章" in capsys.readouterr().out
    conn = store.connect(db)
    store.set_status(conn, URL, Status.FILTERED)
    conn.close()
    assert cli.main(args) == cli.EXIT_ERROR
    assert "DEEPL_API_KEY" in capsys.readouterr().err


class DownScorer:
    version = "v1@down"

    def score(self, title: str, excerpt: str) -> Score:
        raise ScorerUnavailableError("503")


def test_backend_unavailable_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _extracted(tmp_path)
    monkeypatch.chdir(tmp_path)
    deps = cli.Deps(scorer=DownScorer())
    assert cli.main(["filter", "--site", "man", "--db", str(db)], deps=deps) == cli.EXIT_UNAVAILABLE
    out = capsys.readouterr().out
    assert "暂时不可用" in out and "下次运行" in out


def test_eval_reports_agreement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _extracted(tmp_path)
    assert cli.main(["filter", "--site", "man", "--db", str(db), "--scorer", "fake"]) == 0
    capsys.readouterr()
    assert cli.main(["eval", "--site", "man", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "可比较 1 条，一致 1 条，一致率 100%" in out and "v1@fake" in out
    assert "还没打分 / 不在状态库" in out


def test_run_with_everything_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "state.db"
    skips = [x for s in cli.STAGES for x in ("--skip", s)]
    assert cli.main(["run", "--site", "man", "--db", str(db), *skips]) == 0
    assert capsys.readouterr().out == ""


def test_fetch_subprocess_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "[fetch] 抓到 0\n".encode(), b"")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    args = argparse.Namespace(
        db="s.db", raw_dir="r", limit=3, fetch_delay=0.5, verbose=False, config_dir="cfg"
    )
    assert cli.fetch_in_subprocess(args, "man") == 0
    assert seen == [
        [
            sys.executable,
            "-m",
            "qbkb.cli",
            "fetch",
            "--site",
            "man",
            "--db",
            "s.db",
            "--raw-dir",
            "r",
            "--limit",
            "3",
            "--fetch-delay",
            "0.5",
            "--config-dir",
            "cfg",
        ]
    ]
