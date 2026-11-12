"""SQLite 状态库：文章状态、段落组成、段落级翻译记忆。

所有阶段都通过这里读写进度，所以流程可以断点续跑，而且是幂等的。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal

from qbkb.models import DiscoveredUrl, Segment, Status

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
# 改了表结构（加列等）就加 1，并在 _migrate 里补升级步骤
SCHEMA_VERSION = 4

log = logging.getLogger(__name__)

# set_status 允许顺带更新的列。url / site / slug / status / updated_at 由本模块自己管理。
UPDATABLE_COLUMNS = frozenset(
    {
        "lastmod",
        "etag",
        "last_modified",
        "http_status",
        "raw_path",
        "body_hash",
        "body_segments",
        "title",
        "title_zh",
        "author",
        "published",
        "tags",
        "category",
        "value_score",
        "evergreen",
        "score_reason",
        "rubric_version",
        "scored_body_hash",
        "translator",
        "translated_at",
        "md_path",
        "qc_issues",
        "section_key",
        "section",
        "summary_zh",
        "summary_body_hash",
        "summary_version",
        "summary_error",
        "summary_at",
        "fetched_at",
        "summary_truncated",
        "error",
        "retries",
    }
)

# 老库升级：CREATE TABLE IF NOT EXISTS 不会给已有的表加列，这里补上。
# (表, 列, 类型定义)。只加列，不删不改，所以可以反复执行。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("articles", "last_modified", "TEXT"),
    ("articles", "qc_issues", "TEXT"),
    ("articles", "body_segments", "INTEGER"),  # v2（Phase 2）
    ("translation_memory", "quality", "TEXT NOT NULL DEFAULT 'ok'"),
    ("articles", "section_key", "TEXT"),  # v3（按栏目采集）
    ("articles", "section", "TEXT"),
    ("articles", "summary_zh", "TEXT"),
    ("articles", "summary_body_hash", "TEXT"),
    ("articles", "summary_version", "TEXT"),
    ("articles", "summary_error", "TEXT"),
    ("articles", "summary_at", "TEXT"),
    ("articles", "fetched_at", "TEXT"),
    ("articles", "summary_truncated", "INTEGER"),
)
# PR #3 时期用 backend 名加这个后缀隔离坏译文；升级时改成 quality = 'rejected'。
_LEGACY_REJECTED_SUFFIX = "#rejected"


class _Keep(Enum):
    """record_failure 的哨兵：区分"没传 http_status"和"传了 None"。"""

    KEEP = 0


type TmQuality = Literal["ok", "rejected"]
TM_OK: Literal["ok"] = "ok"
TM_REJECTED: Literal["rejected"] = "rejected"

type Value = str | int | float | None


@dataclass(frozen=True, slots=True)
class TmEntry:
    tgt_html: str
    quality: TmQuality


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（不存在就创建）状态库，并确保表结构存在。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """按 PRAGMA user_version 升级老库；已经是最新版本就什么都不做（不扫表）。"""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version >= SCHEMA_VERSION:
        return
    with conn:
        for table, column, decl in _ADDED_COLUMNS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column in existing:
                continue
            try:
                # 表名、列名来自上面的常量，不是外部输入
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError as e:
                # 另一个进程刚好同时在升级同一个库，列已经被它加上了
                if "duplicate column name" not in str(e):
                    raise
        _convert_legacy_rejected(conn)
        # PRAGMA 不能带参数；SCHEMA_VERSION 是常量
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _convert_legacy_rejected(conn: sqlite3.Connection) -> None:
    """PR #3 时期的隔离区（backend 带 #rejected 后缀）→ quality = rejected。

    主键是 (src_hash, backend)，同一段落已有合格译文时两条放不进同一个键：保留合格的，
    坏译文丢弃并记日志（它本来就不会被复用，只是人工复查时看不到了）。
    """
    pattern = "%" + _LEGACY_REJECTED_SUFFIX
    n = len(_LEGACY_REJECTED_SUFFIX)
    dropped = conn.execute(
        "SELECT old.src_hash, old.backend FROM translation_memory AS old"
        " JOIN translation_memory AS cur"
        " ON cur.src_hash = old.src_hash"
        " AND cur.backend = substr(old.backend, 1, length(old.backend) - ?)"
        " WHERE old.backend LIKE ?",
        (n, pattern),
    ).fetchall()
    for r in dropped:
        log.warning(
            "升级翻译记忆：%s 在 %s 下已有合格译文，丢弃旧隔离区里的坏译文", r[0], r[1][:-n]
        )
    conn.execute(
        "INSERT OR IGNORE INTO translation_memory"
        " (src_hash, backend, src_html, tgt_html, glossary_version, quality, created_at)"
        " SELECT src_hash, substr(backend, 1, length(backend) - ?), src_html, tgt_html,"
        " glossary_version, ?, created_at FROM translation_memory WHERE backend LIKE ?",
        (n, TM_REJECTED, pattern),
    )
    conn.execute("DELETE FROM translation_memory WHERE backend LIKE ?", (pattern,))


def upsert_discovered(conn: sqlite3.Connection, d: DiscoveredUrl) -> bool:
    """记录 discover 发现的 URL。

    返回 True 表示需要（重新）处理：URL 是新的，或者 lastmod 变了。

    lastmod 变了的文章退回 discovered 状态完整重抓：
    - 清掉 etag、last_modified，避免条件请求拿到 304 却已经丢了原来的流程位置；
    - 清掉 error、retries，原文更新后给失败过的文章一次新机会。
    value_score、scored_body_hash 等保留：重新抽取会覆盖 body_hash，filter 比较
    body_hash 与 scored_body_hash，相同就复用打分，不重复花 LLM 额度；
    translate 靠段落级翻译记忆复用，不花 DeepL 额度。

    INSERT ... ON CONFLICT 让"查询 + 插入"成为一条语句，两个 discover 并发也不会撞主键。
    """
    now = _now()
    with conn:
        cur = conn.execute(
            "INSERT INTO articles (url, site, slug, status, lastmod, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (url) DO NOTHING",
            (d.url, d.site, d.slug, Status.DISCOVERED.value, d.lastmod, now),
        )
        if cur.rowcount == 1:
            return True
        if d.lastmod is None:
            return False
        cur = conn.execute(
            "UPDATE articles SET lastmod = ?, status = ?, etag = NULL, last_modified = NULL,"
            " error = NULL,"
            " retries = 0, updated_at = ? WHERE url = ? AND lastmod IS NOT ?",
            (d.lastmod, Status.DISCOVERED.value, now, d.url, d.lastmod),
        )
        return cur.rowcount == 1


def get_article(conn: sqlite3.Connection, url: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM articles WHERE url = ?", (url,)
    ).fetchone()
    return row


def list_by_status(conn: sqlite3.Connection, status: Status, site: str | None = None) -> list[str]:
    """某个状态下的全部 URL，按 URL 排序，保证每次运行的处理顺序一致。"""
    if site is None:
        rows = conn.execute(
            "SELECT url FROM articles WHERE status = ? ORDER BY url", (status.value,)
        )
    else:
        rows = conn.execute(
            "SELECT url FROM articles WHERE status = ? AND site = ? ORDER BY url",
            (status.value, site),
        )
    return [r["url"] for r in rows]


def count_by_status(conn: sqlite3.Connection, site: str | None = None) -> dict[Status, int]:
    """每个状态有多少篇文章（没有文章的状态不出现）。"""
    if site is None:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM articles GROUP BY status")
    else:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM articles WHERE site = ? GROUP BY status", (site,)
        )
    return {Status(r["status"]): int(r["n"]) for r in rows}


def list_rows(
    conn: sqlite3.Connection, statuses: Sequence[Status], site: str | None = None
) -> list[sqlite3.Row]:
    """若干状态下的整行记录，按 URL 排序（status、index.md 用）。"""
    if not statuses:
        return []
    marks = ", ".join("?" for _ in statuses)
    params: list[str] = [s.value for s in statuses]
    sql = f"SELECT * FROM articles WHERE status IN ({marks})"
    if site is not None:
        sql += " AND site = ?"
        params.append(site)
    return list(conn.execute(sql + " ORDER BY url", params))


def set_status(conn: sqlite3.Connection, url: str, status: Status, **fields: Value) -> None:
    """把文章推进到新状态，并顺带更新若干列。未知列直接报错，防止拼错字段名。"""
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"不能更新的列: {sorted(unknown)}")
    # 列名只来自 UPDATABLE_COLUMNS 白名单，值全部走参数绑定，没有 SQL 注入风险。
    assignments = ["status = ?", "updated_at = ?", *(f"{col} = ?" for col in fields)]
    params: list[Value] = [status.value, _now(), *fields.values(), url]
    with conn:
        cur = conn.execute(f"UPDATE articles SET {', '.join(assignments)} WHERE url = ?", params)
    if cur.rowcount == 0:
        raise KeyError(url)


def set_fields(conn: sqlite3.Connection, url: str, **fields: Value) -> None:
    """只更新若干列，不改状态、不动 updated_at（补记栏目等不算流程推进）。"""
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"不能更新的列: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{col} = ?" for col in fields)
    with conn:
        cur = conn.execute(
            f"UPDATE articles SET {assignments} WHERE url = ?", [*fields.values(), url]
        )
    if cur.rowcount == 0:
        raise KeyError(url)


def site_rows(conn: sqlite3.Connection, site: str) -> list[sqlite3.Row]:
    """这个站点的全部文章（任意状态），按 URL 排序。"""
    return list(conn.execute("SELECT * FROM articles WHERE site = ? ORDER BY url", (site,)))


def record_failure(
    conn: sqlite3.Connection,
    url: str,
    error: str,
    *,
    http_status: int | None | Literal[_Keep.KEEP] = _Keep.KEEP,
) -> int:
    """标记失败并累加重试次数，返回新的重试次数。

    传了 http_status（包括 None，表示没拿到响应）就在同一个事务里一并写入，
    fetch 不用先 set_status 一次；不传则保留原值（extract、translate 的失败）。
    """
    update_status = http_status is not _Keep.KEEP
    with conn:
        row = conn.execute(
            "UPDATE articles SET status = ?, error = ?, retries = retries + 1, updated_at = ?,"
            " http_status = CASE WHEN ? THEN ? ELSE http_status END"
            " WHERE url = ? RETURNING retries",
            (
                Status.FAILED.value,
                error,
                _now(),
                update_status,
                None if http_status is _Keep.KEEP else http_status,
                url,
            ),
        ).fetchone()
    if row is None:
        raise KeyError(url)
    return int(row["retries"])


def replace_segments(conn: sqlite3.Connection, url: str, segments: Sequence[Segment]) -> None:
    """用新的段落列表整体替换文章的段落组成。"""
    with conn:
        conn.execute("DELETE FROM segments WHERE article_url = ?", (url,))
        conn.executemany(
            "INSERT INTO segments (article_url, idx, src_hash, src_html) VALUES (?, ?, ?, ?)",
            [(url, s.idx, s.src_hash, s.html) for s in segments],
        )


def get_segments(conn: sqlite3.Connection, url: str) -> list[Segment]:
    """按顺序取文章的全部段落（translate / export 的输入）。"""
    rows = conn.execute(
        "SELECT idx, src_html FROM segments WHERE article_url = ? ORDER BY idx", (url,)
    )
    return [Segment(idx=int(r["idx"]), html=str(r["src_html"])) for r in rows]


def get_segment_hashes(conn: sqlite3.Connection, url: str) -> list[str]:
    rows = conn.execute("SELECT src_hash FROM segments WHERE article_url = ? ORDER BY idx", (url,))
    return [r["src_hash"] for r in rows]


def tm_lookup(
    conn: sqlite3.Connection, src_hash: str, backend: str, glossary_version: str | None
) -> TmEntry | None:
    """查翻译记忆，连同质量标记一起返回（包括 rejected 的）。"""
    row = conn.execute(
        "SELECT tgt_html, quality FROM translation_memory"
        " WHERE src_hash = ? AND backend = ? AND glossary_version IS ?",
        (src_hash, backend, glossary_version),
    ).fetchone()
    if row is None:
        return None
    quality = TM_REJECTED if row["quality"] == TM_REJECTED else TM_OK
    return TmEntry(str(row["tgt_html"]), quality)


def tm_get(
    conn: sqlite3.Connection, src_hash: str, backend: str, glossary_version: str | None
) -> str | None:
    """查翻译记忆。命中合格（quality = ok）的译文就返回，不用再花额度。

    术语表版本不同视为未命中：术语表升级后，旧译文可能用的是旧译名。
    rejected 的译文不返回，避免被当成正常译文复用。
    """
    entry = tm_lookup(conn, src_hash, backend, glossary_version)
    return entry.tgt_html if entry is not None and entry.quality == TM_OK else None


def tm_put(
    conn: sqlite3.Connection,
    src_hash: str,
    backend: str,
    src_html: str,
    tgt_html: str,
    glossary_version: str | None = None,
    *,
    quality: TmQuality = TM_OK,
) -> None:
    """写入翻译记忆。

    rejected 不覆盖同一术语版本下已有的 ok 译文：那份译文已经花过额度，人工复查时还要看；
    术语版本不同时照常覆盖（旧版本的译文按规则已经不会命中）。ok 总是可以覆盖 rejected。
    """
    if quality not in (TM_OK, TM_REJECTED):
        raise ValueError(f"quality 必须是 {TM_OK!r} 或 {TM_REJECTED!r}")
    with conn:
        conn.execute(
            "INSERT INTO translation_memory"
            " (src_hash, backend, src_html, tgt_html, glossary_version, quality, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (src_hash, backend) DO UPDATE SET"
            " tgt_html = excluded.tgt_html,"
            " glossary_version = excluded.glossary_version,"
            " quality = excluded.quality,"
            " created_at = excluded.created_at"
            " WHERE excluded.quality = 'ok' OR translation_memory.quality != 'ok'"
            " OR translation_memory.glossary_version IS NOT excluded.glossary_version",
            (src_hash, backend, src_html, tgt_html, glossary_version, quality, _now()),
        )
