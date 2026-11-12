"""qbkb sections（栏目调研）和 qbkb status 的栏目部分。构造的站点，全部离线，不调用 LLM。"""

from __future__ import annotations

import argparse
import csv
import sqlite3
from pathlib import Path

import pytest
import yaml
from sections_site import BASE, SECTIONS, page, write_config

from qbkb import cli, store
from qbkb.fetch.outcome import Kind, Outcome
from qbkb.fetch.process import apply_outcome
from qbkb.models import DiscoveredUrl, Status
from qbkb.sections import survey


def _row(conn: sqlite3.Connection, path: str, key: str | None, status: Status) -> str:
    url = BASE + path
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", path.rsplit("/", 1)[-1]))
    if key is not None or status is not Status.DISCOVERED:
        store.set_status(conn, url, status, section_key=key, title=f"Title {path}")
    return url


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = write_config(root, SECTIONS)
        self.db = root / "state.db"
        self.csv = root / "out" / "ex.csv"
        conn = store.connect(self.db)
        for i in range(35):
            _row(conn, f"/insights/art-{i:02d}", "ARTICLE", Status.EXTRACTED)
        for i in range(3):
            _row(conn, f"/insights/wk-{i}", "ARTICLE | WEEKLY NOTES", Status.UNSELECTED)
        for i in range(4):
            _row(conn, f"/insights/weekly-notes-{i}", None, Status.DISCOVERED)  # 估计
        for i in range(2):
            _row(conn, f"/insights/misc-{i}", None, Status.DISCOVERED)  # 看不出栏目
        _row(conn, "/insights/macro-blog/a", None, Status.DISCOVERED)  # 网址前缀就能定
        _row(conn, "/insights/pod-0", "PODCAST | NEW SHOW", Status.UNSELECTED)
        conn.close()

    def argv(self, *extra: str) -> list[str]:
        return [
            "sections",
            "--site",
            "ex",
            "--db",
            str(self.db),
            "--config-dir",
            str(self.cfg),
            "--csv",
            str(self.csv),
            "--raw-dir",
            str(self.root / "raw"),
            *extra,
        ]

    def rows(self) -> list[dict[str, str]]:
        return survey.read_csv(self.csv)

    def mark(self, marks: dict[str, str]) -> None:
        rows = self.rows()
        for r in rows:
            r["要不要"] = marks.get(r["栏目id"], r["要不要"])
        with self.csv.open("w", encoding="utf-8-sig", newline="") as f:
            fields: list[str] = list(survey.COLUMNS)
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)


def test_sections_lists_counts_samples_and_estimates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv("--sample", "5")) == cli.EXIT_OK
    out = capsys.readouterr().out
    rows = env.rows()
    by: dict[str, int] = {}
    for r in rows:
        by[r["栏目id"]] = by.get(r["栏目id"], 0) + 1
    # 35 篇的栏目抽 5 篇；不超过 30 篇的栏目列全部；新栏目也列出来
    assert by == {"article": 5, "weekly": 3, "macro": 1, "new:PODCAST | NEW SHOW": 1}
    assert {r["是否采集"] for r in rows if r["栏目id"] == "article"} == {"是"}
    summary = {
        r[0]: r for r in csv.reader(env.csv.with_name("ex-summary.csv").open(encoding="utf-8-sig"))
    }
    assert summary["weekly"][3] == "3" and summary["weekly"][4] == "约 4"  # 实际 3，估计 4
    assert summary["unclassified"][4] == "约 2"
    assert "估计 = 没抓页面、按网址猜的" in out


def test_sampling_is_reproducible_and_prefix_stable(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv("--sample", "5")) == 0
    first = [r["网址"] for r in env.rows() if r["栏目id"] == "article"]
    env.csv.unlink()
    assert cli.main(env.argv("--sample", "5")) == 0
    assert [r["网址"] for r in env.rows() if r["栏目id"] == "article"] == first
    # 补抽：N 调大，原来抽到的都还在
    assert cli.main(env.argv("--sample", "8", "--resample")) == 0
    more = [r["网址"] for r in env.rows() if r["栏目id"] == "article"]
    assert len(more) == 8 and more[:5] == first
    # 换种子是另一批
    env.csv.unlink()
    assert cli.main(env.argv("--sample", "5", "--seed", "7")) == 0
    assert [r["网址"] for r in env.rows() if r["栏目id"] == "article"] != first


def test_marks_give_ratio_and_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv("--sample", "5")) == 0
    env.mark({"article": "要", "weekly": "不要"})
    capsys.readouterr()
    assert cli.main(env.argv("--sample", "5")) == 0  # 已有抽样表：只读标记
    out = capsys.readouterr().out
    assert "读取已有的抽样表" in out
    assert "超过七成" in out and "不到七成" in out
    assert "article: survey: {sampled: 5, wanted: 5, ratio: 1.00" in out
    # 重新抽样时，已标的按网址保留
    assert cli.main(env.argv("--sample", "6", "--resample")) == 0
    marks = [r["要不要"] for r in env.rows() if r["栏目id"] == "article"]
    assert marks.count("要") == 5 and marks.count("") == 1


def test_verdict_bands() -> None:
    def v(yes: int, no: int) -> str:
        return survey.Verdict("s", yes + no, yes, no).conclusion

    assert v(17, 3).startswith("超过七成")  # 85% > 70%
    assert v(14, 6).startswith("不到七成")  # 恰好 70%
    assert v(13, 7).startswith("不到七成")  # 65%
    assert v(11, 9).startswith("不到七成")  # 55%
    assert v(0, 0) == "还没标"
    assert survey.parse_mark(" Y ") is True and survey.parse_mark("不要") is False
    assert survey.parse_mark("maybe") is None


def test_fetch_samples_through_fetch_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--fetch N：固定种子随机挑 N 篇没判定栏目的页面，走正常的 fetch 阶段，再抽取判定。"""
    env = Env(tmp_path)
    got: list[list[str]] = []

    def fake_fetch(args: argparse.Namespace, site: str) -> int:
        urls = Path(args.url_file).read_text(encoding="utf-8").split()
        got.append(urls)
        conn = store.connect(args.db)
        for u in urls:
            body = page("T", "ARTICLE | 2 MIN | WEEKLY NOTES").encode("utf-8")
            apply_outcome(conn, u, Outcome(Kind.FETCHED, 200), body, Path(args.raw_dir))
        conn.close()
        return 0

    deps = cli.Deps(fetch_runner=fake_fetch)
    # 库里有一个新栏目（PODCAST | NEW SHOW），抽取阶段提醒 → 需要处理
    assert cli.main(env.argv("--fetch", "3"), deps=deps) == cli.EXIT_ATTENTION
    assert len(got) == 1 and len(got[0]) == 3
    assert all("macro-blog" not in u for u in got[0])  # 网址前缀能定的不用抓
    summary = {
        r[0]: r for r in csv.reader(env.csv.with_name("ex-summary.csv").open(encoding="utf-8-sig"))
    }
    assert summary["weekly"][3] == "6"  # 3 + 新抓的 3
    # 同一个种子挑的是同一批
    env2 = Env(tmp_path / "again")
    got2: list[list[str]] = []

    def record(args: argparse.Namespace, site: str) -> int:
        got2.append(Path(args.url_file).read_text(encoding="utf-8").split())
        return 0

    cli.main(env2.argv("--fetch", "3"), deps=cli.Deps(fetch_runner=record))
    assert [u.removeprefix(BASE) for u in got2[0]] == [u.removeprefix(BASE) for u in got[0]]


def test_fetch_respects_compliance_gate(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = Env(tmp_path)
    path = env.cfg / "sites" / "ex.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["compliance"]["decision"] = "permission_needed"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    called: list[str] = []
    deps = cli.Deps(fetch_runner=lambda a, s: called.append(s) or 0)
    assert cli.main(env.argv("--fetch", "2"), deps=deps) == cli.EXIT_ERROR
    assert called == [] and "不能抓取" in capsys.readouterr().err


def test_sections_requires_site(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["sections", "--db", str(tmp_path / "x.db")]) == cli.EXIT_ERROR
    assert "--site" in capsys.readouterr().err


def test_status_lists_sections_and_new_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = Env(tmp_path)
    argv = ["status", "--site", "ex", "--db", str(env.db), "--config-dir", str(env.cfg)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "unselected" in out
    assert "Articles（article）：35 篇，采集" in out
    assert "Weekly Notes（weekly）：3 篇，不采集" in out
    assert "Macro Blog（macro）：1 篇，不采集" in out  # 网址前缀就能定，没抓也算
    assert "还没判定栏目的 6 篇不计" in out
    assert "新栏目（配置里没有，只记录不采集，1 个）" in out and "PODCAST | NEW SHOW：1 篇" in out


def test_translate_hint_when_nothing_selected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    none = {**SECTIONS, "list": [{**s, "collect": False} for s in SECTIONS["list"]]}
    cfg = write_config(tmp_path, none)
    argv = ["translate", "--site", "ex", "--db", str(tmp_path / "s.db"), "--config-dir", str(cfg)]
    assert cli.main(argv) == 0
    assert "还没有勾选任何采集栏目" in capsys.readouterr().out


def test_extract_reports_new_sections_as_attention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = write_config(tmp_path, SECTIONS)
    db = tmp_path / "s.db"
    conn = store.connect(db)
    url = BASE + "/insights/new-1"
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", "new-1"))
    apply_outcome(
        conn,
        url,
        Outcome(Kind.FETCHED, 200),
        page("N", "ARTICLE | 3 MIN | BRAND NEW").encode("utf-8"),
        tmp_path / "raw",
    )
    conn.close()
    argv = ["extract", "--site", "ex", "--db", str(db), "--config-dir", str(cfg)]
    assert cli.main(argv) == cli.EXIT_ATTENTION
    out = capsys.readouterr().out
    assert "发现 1 个配置里没有的栏目（只记录，不采集" in out and "ARTICLE | BRAND NEW：1 篇" in out


def test_resample_keeps_marked_rows_when_seed_or_size_changes(tmp_path: Path) -> None:
    env = Env(tmp_path)
    assert cli.main(env.argv("--sample", "5")) == 0
    env.mark({"article": "要"})
    previous = {r["网址"] for r in env.rows() if r["栏目id"] == "article"}
    assert cli.main(env.argv("--sample", "1", "--seed", "7", "--resample")) == 0
    rows = env.rows()
    kept = {r["网址"] for r in rows if r["要不要"] == "要"}
    assert previous <= kept
    urls = [r["网址"] for r in rows]
    assert len(urls) == len(set(urls))
