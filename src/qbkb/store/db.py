"""SQLite 状态库：文章状态、段落组成、段落级翻译记忆。

所有阶段都通过这里读写进度，所以流程可以断点续跑，而且是幂等的。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from qbkb.models import DiscoveredUrl, Segment, Status

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# set_status 允许顺带更新的列。url / site / slug / status / updated_at 由本模块自己管理。
UPDATABLE_COLUMNS = frozenset(
    {
        "lastmod",
        "etag",
        "http_status",
        "raw_path",
        "body_hash",
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
        "error",
        "retries",
    }
)

type Value = str | int | float | None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """打开（不存在就创建）状态库，并确保表结构存在。"""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def upsert_discovered(conn: sqlite3.Connection, d: DiscoveredUrl) -> bool:
    """记录 discover 发现的 URL。

    返回 True 表示需要（重新）处理：URL 是新的，或者 lastmod 变了。

    lastmod 变了的文章退回 discovered 状态完整重抓：
    - 清掉 etag，避免条件请求拿到 304 却已经丢了原来的流程位置；
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
            "UPDATE articles SET lastmod = ?, status = ?, etag = NULL, error = NULL,"
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


def record_failure(conn: sqlite3.Connection, url: str, error: str) -> int:
    """标记失败并累加重试次数，返回新的重试次数。"""
    with conn:
        row = conn.execute(
            "UPDATE articles SET status = ?, error = ?, retries = retries + 1, updated_at = ?"
            " WHERE url = ? RETURNING retries",
            (Status.FAILED.value, error, _now(), url),
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


def tm_get(
    conn: sqlite3.Connection, src_hash: str, backend: str, glossary_version: str | None
) -> str | None:
    """查翻译记忆。命中就返回译文，不用再花额度。

    术语表版本不同视为未命中：术语表升级后，旧译文可能用的是旧译名。
    """
    row = conn.execute(
        "SELECT tgt_html FROM translation_memory"
        " WHERE src_hash = ? AND backend = ? AND glossary_version IS ?",
        (src_hash, backend, glossary_version),
    ).fetchone()
    return None if row is None else str(row["tgt_html"])


def tm_put(
    conn: sqlite3.Connection,
    src_hash: str,
    backend: str,
    src_html: str,
    tgt_html: str,
    glossary_version: str | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT INTO translation_memory"
            " (src_hash, backend, src_html, tgt_html, glossary_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (src_hash, backend) DO UPDATE SET"
            " tgt_html = excluded.tgt_html,"
            " glossary_version = excluded.glossary_version,"
            " created_at = excluded.created_at",
            (src_hash, backend, src_html, tgt_html, glossary_version, _now()),
        )
