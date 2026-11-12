"""栏目调研（`qbkb sections`，design.md 4.9）：数篇数、随机抽样、读回人工标记、算占比。

不调用任何 LLM。抽样是确定的：同一个种子、同样的栏目成员，抽到的永远是同一批；
把每个栏目的成员按网址排序后用 random.Random(f"{种子}:{栏目}") 洗牌、取前 N 篇，
所以把 N 调大（补抽）时，原来抽到的那些仍在里面，已经标好的不会白标。
"""

from __future__ import annotations

import csv
import random
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from qbkb import store
from qbkb.sections.rules import SectionRules

DEFAULT_SAMPLE = 20
DEFAULT_SEED = 2026
# 成员不超过这么多的栏目看全部，不抽样（design.md 4.4）
FULL_BELOW = 30
# 占比结论的分界：高于 HIGH 是"超过七成"，低于 LOW 是"不够"，中间是"七成上下"
HIGH = 0.8
LOW = 0.6

NEW_PREFIX = "new:"
UNKNOWN_ID = "unclassified"

COLUMNS = ("栏目id", "栏目", "是否采集", "网址", "英文标题", "发布日期", "要不要", "备注")
SUMMARY_COLUMNS = (
    "栏目id",
    "栏目",
    "是否采集",
    "已判定篇数",
    "估计篇数（未抓取）",
    "抽样",
    "已标",
    "要",
    "不要",
    "占比",
    "结论",
)
YES = frozenset({"要", "是", "y", "yes", "1", "true", "✓", "√"})
NO = frozenset({"不要", "否", "n", "no", "0", "false", "✗", "×"})


@dataclass(slots=True)
class Member:
    url: str
    title: str
    published: str


@dataclass(slots=True)
class SectionTally:
    id: str
    name: str
    collect: bool | None  # None：配置里没有（新栏目 / 未分类）
    members: list[Member] = field(default_factory=list[Member])  # 已判定属于这个栏目的
    estimated: int = 0  # 没抓页面、按网址估计属于这个栏目的

    @property
    def known(self) -> int:
        return len(self.members)


@dataclass(frozen=True, slots=True)
class Verdict:
    section: str
    marked: int
    yes: int
    no: int

    @property
    def ratio(self) -> float | None:
        return None if self.marked == 0 else self.yes / self.marked

    @property
    def conclusion(self) -> str:
        r = self.ratio
        if r is None:
            return "还没标"
        if r > HIGH:
            return "超过七成：可以整栏采集"
        if r < LOW:
            return "不够：不整栏采集"
        return "七成上下：补抽 20–30 篇，或请老板决定"


def tally(conn: sqlite3.Connection, site: str, rules: SectionRules) -> list[SectionTally]:
    """每个栏目的成员（已判定的）和估计篇数（没判定的按网址估）。

    顺序：配置顺序 → 新栏目 → 未分类。
    """
    out = {s.id: SectionTally(s.id, s.name, s.collect) for s in rules.sections}
    unknown = SectionTally(UNKNOWN_ID, "未分类（没抓取，网址看不出栏目）", None)
    extra: dict[str, SectionTally] = {}
    for row in store.site_rows(conn, site):
        url = str(row["url"])
        key = row["section_key"]
        by_url = rules.by_url(url)
        if key is None and by_url is None:
            guess = rules.estimate(url)
            (out[guess.id] if guess is not None else unknown).estimated += 1
            continue
        member = Member(url, str(row["title"] or ""), str(row["published"] or ""))
        section = rules.classify(url, key)
        if section is not None:
            out[section.id].members.append(member)
            continue
        label = key or "（页面上读不到栏目信息）"
        t = extra.setdefault(label, SectionTally(NEW_PREFIX + label, f"新栏目：{label}", None))
        t.members.append(member)
    result = list(out.values()) + [extra[k] for k in sorted(extra)]
    if unknown.estimated:
        result.append(unknown)
    return result


def sample(t: SectionTally, n: int, seed: int) -> list[Member]:
    members = sorted(t.members, key=lambda m: m.url)
    if len(members) <= FULL_BELOW:
        return members
    random.Random(f"{seed}:{t.id}").shuffle(members)
    return members[:n]


def pick_unclassified(
    conn: sqlite3.Connection, site: str, rules: SectionRules, n: int, seed: int
) -> list[str]:
    """为了抽样要抓的页面：还没判定栏目、状态是 discovered 的，固定种子随机取 n 篇。"""
    urls = [
        str(r["url"])
        for r in store.site_rows(conn, site)
        if r["status"] == "discovered"
        and r["section_key"] is None
        and rules.by_url(r["url"]) is None
    ]
    random.Random(f"{seed}:fetch").shuffle(urls)
    return urls[:n]


def parse_mark(value: str) -> bool | None:
    v = value.strip().lower()
    if v in YES:
        return True
    if v in NO:
        return False
    return None


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def _write(path: Path, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # 带 BOM：Excel 直接打开不乱码
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)
    tmp.replace(path)


def _collect_label(c: bool | None) -> str:
    return "—" if c is None else ("是" if c else "否")


def write_samples(
    path: Path,
    tallies: Sequence[SectionTally],
    n: int,
    seed: int,
    old: Sequence[dict[str, str]] = (),
) -> int:
    """写抽样表；old 里已经标过的（按网址）原样带过来。返回写了多少行。"""
    marks = {r.get("网址", ""): (r.get("要不要", ""), r.get("备注", "")) for r in old}
    rows: list[list[str]] = []
    for t in tallies:
        chosen = sample(t, n, seed)
        seen = {m.url for m in chosen}
        # 重新抽样仍保留当前栏目内已经投入人工判断或备注的成员。
        chosen += [
            m
            for m in t.members
            if m.url not in seen and any(v.strip() for v in marks.get(m.url, ("", "")))
        ]
        for m in chosen:
            mark, note = marks.get(m.url, ("", ""))
            if not note and not m.title:
                note = "还没抓取，只有网址"
            rows.append(
                [
                    t.id,
                    t.name,
                    _collect_label(t.collect),
                    m.url,
                    m.title,
                    m.published[:10],
                    mark,
                    note,
                ]
            )
    _write(path, COLUMNS, rows)
    return len(rows)


def verdicts(rows: Iterable[dict[str, str]]) -> tuple[dict[str, Verdict], list[str]]:
    """按栏目统计人工标记。返回 (栏目 id → 结论, 看不懂的标记)。"""
    counts: dict[str, list[int]] = {}
    bad: list[str] = []
    for r in rows:
        sid = r.get("栏目id", "")
        c = counts.setdefault(sid, [0, 0])
        raw = r.get("要不要", "") or ""
        if not raw.strip():
            continue
        mark = parse_mark(raw)
        if mark is None:
            bad.append(f"{r.get('网址', '')}：{raw!r}")
        elif mark:
            c[0] += 1
        else:
            c[1] += 1
    return {sid: Verdict(sid, y + n, y, n) for sid, (y, n) in counts.items()}, bad


def summary_rows(
    tallies: Sequence[SectionTally], vs: dict[str, Verdict], sampled: dict[str, int]
) -> list[list[str]]:
    rows: list[list[str]] = []
    for t in tallies:
        v = vs.get(t.id, Verdict(t.id, 0, 0, 0))
        ratio = "" if v.ratio is None else f"{v.ratio:.0%}"
        rows.append(
            [
                t.id,
                t.name,
                _collect_label(t.collect),
                str(t.known),
                f"约 {t.estimated}" if t.estimated else "0",
                str(sampled.get(t.id, 0)),
                str(v.marked),
                str(v.yes),
                str(v.no),
                ratio,
                v.conclusion if sampled.get(t.id) else "没有可抽样的文章（先抓一些页面）",
            ]
        )
    return rows


def write_summary(path: Path, rows: Sequence[Sequence[str]]) -> None:
    _write(path, SUMMARY_COLUMNS, rows)
