"""按栏目配置推进 / 退回文章状态（design.md 4.9）。不访问网络，可以反复执行。

- discovered：网址前缀就能定栏目、且这个栏目不采集 → unselected（不抓取）；
- extracted：栏目不在采集列表里（包括配置里没有的新栏目）→ unselected；
- unselected：栏目改成采集了 → 抽取过的回到 extracted，没抓过的回到 discovered；
- 其他状态（已翻译、已发布、失败……）不改状态，只补记栏目 id。

改了栏目配置（勾选 / 取消、改 key）之后，下一次 discover / extract / translate 都会重新对一遍。
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

from qbkb import store
from qbkb.config import SiteConfig
from qbkb.models import Status
from qbkb.sections.rules import URL_KEY_PREFIX, SectionRules, load_section_rules


@dataclass(slots=True)
class ApplyStats:
    unselected: int = 0  # 这次标成"未选栏目"的
    reselected: int = 0  # 栏目改成采集后回到流程的


@dataclass(frozen=True, slots=True)
class NewSection:
    """配置里没有的栏目：只提醒，不采集。"""

    key: str  # "" 表示页面上读不到栏目信息
    count: int
    example: str

    @property
    def label(self) -> str:
        return self.key or "（页面上读不到栏目信息）"


def apply_sections(
    conn: sqlite3.Connection, site: SiteConfig, rules: SectionRules | None = None
) -> ApplyStats:
    rules = rules or load_section_rules(site)
    stats = ApplyStats()
    for row in store.site_rows(conn, site.name):
        url = str(row["url"])
        status = Status(row["status"])
        key: str | None = row["section_key"]
        by_url = rules.by_url(url) if rules.enabled else None
        if by_url is not None:
            key = by_url.url_key
        section = rules.classify(url, key) if rules.enabled else None
        sid = None if section is None else section.id
        fields: dict[str, str | None] = {}
        if key != row["section_key"]:
            fields["section_key"] = key
        if sid != row["section"]:
            fields["section"] = sid
        collect = rules.collects(url, key)
        if status is Status.DISCOVERED:
            # 只有网址就能定栏目的才在抓取前处理；其余要抓到页面才知道
            if by_url is not None and not collect:
                store.set_status(conn, url, Status.UNSELECTED, **fields)
                stats.unselected += 1
                continue
        elif status is Status.EXTRACTED:
            # key 为 None：老数据还没按 v2 规则判定，等 extract 重新抽取，先不动
            if not collect and (key is not None or by_url is not None):
                store.set_status(conn, url, Status.UNSELECTED, **fields)
                stats.unselected += 1
                continue
        elif status is Status.UNSELECTED and collect:  # 回到流程
            back = Status.EXTRACTED if row["body_hash"] else Status.DISCOVERED
            store.set_status(conn, url, back, **fields)
            stats.reselected += 1
            continue
        if fields:
            store.set_fields(conn, url, **fields)
    return stats


def translatable(
    conn: sqlite3.Connection, site: SiteConfig, rules: SectionRules | None = None
) -> list[str]:
    """待翻译的文章：extracted（以及可选的 qbkb filter 留下的 filtered），且栏目在采集列表里。

    分栏目的站点里，还没按 v2 规则判定栏目的老数据（section_key 为 NULL）不翻译，
    等 extract 重新抽取判定后再说。
    """
    rules = rules or load_section_rules(site)
    out: list[str] = []
    for row in store.list_rows(conn, [Status.EXTRACTED, Status.FILTERED], site.name):
        url = str(row["url"])
        if rules.collects(url, row["section_key"]):
            out.append(url)
    return out


def new_sections(
    conn: sqlite3.Connection, site: SiteConfig, rules: SectionRules | None = None
) -> list[NewSection]:
    """已经判定过栏目、但配置里没有对应栏目的标识，按篇数从多到少。不分栏目的站点返回空。"""
    rules = rules or load_section_rules(site)
    if not rules.enabled:
        return []
    counts: Counter[str] = Counter()
    example: dict[str, str] = {}
    for row in store.site_rows(conn, site.name):
        key = row["section_key"]
        url = str(row["url"])
        if key is None or key.startswith(URL_KEY_PREFIX) or rules.classify(url, key) is not None:
            continue
        counts[key] += 1
        example.setdefault(key, url)
    return [NewSection(k, n, example[k]) for k, n in sorted(counts.items(), key=lambda x: -x[1])]
