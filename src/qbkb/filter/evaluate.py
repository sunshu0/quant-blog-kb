"""评测：LLM 判断和人工判断的一致率（design.md 4.4 L3）。

LLM 判断取状态库里已有的打分（value_score + category，按 filters.yaml 的阈值和收录类别），
不重新打分、不花钱；人工覆盖（overrides.yaml）不算 LLM 判断。
改评分标准时：换 rubric_version → 只对评测集里的文章重跑 filter → 再看一致率。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from qbkb import store
from qbkb.filter.rules import Decision, FilterConfig


@dataclass(frozen=True, slots=True)
class Disagreement:
    url: str
    human: Decision
    llm: Decision
    score: float
    category: str
    reason: str


@dataclass(slots=True)
class EvalReport:
    total: int = 0  # 评测集条数
    compared: int = 0  # 状态库里有打分、能比较的条数
    agree: int = 0
    # 以"收录"为正类的混淆矩阵
    true_pos: int = 0
    false_pos: int = 0  # 人工不收、LLM 收了
    false_neg: int = 0  # 人工收、LLM 没收
    true_neg: int = 0
    missing: list[str] = field(default_factory=list)  # 状态库里没有、或还没打过分
    disagreements: list[Disagreement] = field(default_factory=list)
    rubric_versions: set[str] = field(default_factory=set[str])

    @property
    def agreement(self) -> float | None:
        return self.agree / self.compared if self.compared else None


def evaluate(
    conn: sqlite3.Connection,
    items: Mapping[str, Decision],
    cfg: FilterConfig,
    *,
    rubric_version: str | None = None,
) -> EvalReport:
    """rubric_version 给了就只比较这个版本的打分（其他版本算 missing）。"""
    report = EvalReport(total=len(items))
    for url, human in sorted(items.items()):
        row = store.get_article(conn, url)
        if row is None or row["value_score"] is None or row["category"] is None:
            report.missing.append(url)
            continue
        version = str(row["rubric_version"] or "")
        if rubric_version is not None and version != rubric_version:
            report.missing.append(url)
            continue
        report.rubric_versions.add(version)
        score, category = float(row["value_score"]), str(row["category"])
        llm = Decision.INCLUDE if cfg.keeps(score, category) else Decision.EXCLUDE
        report.compared += 1
        match (human, llm):
            case (Decision.INCLUDE, Decision.INCLUDE):
                report.true_pos += 1
            case (Decision.EXCLUDE, Decision.INCLUDE):
                report.false_pos += 1
            case (Decision.INCLUDE, Decision.EXCLUDE):
                report.false_neg += 1
            case _:
                report.true_neg += 1
        if human is llm:
            report.agree += 1
        else:
            reason = str(row["score_reason"] or "")
            report.disagreements.append(Disagreement(url, human, llm, score, category, reason))
    return report
