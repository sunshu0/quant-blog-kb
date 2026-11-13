"""中文摘要只用Mock，不读取密钥、不访问外部API。"""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from qbkb import cli, store
from qbkb.config import SiteConfig
from qbkb.export.tables import records
from qbkb.filter.rules import Rubric
from qbkb.filter.scorers import DeepSeekScorer
from qbkb.models import DiscoveredUrl, Segment, Status
from qbkb.summarize import PROMPT, DeepSeekSummarizer, run, valid_summary
from qbkb.translate import Glossary

SITE = SiteConfig("ex", "allowed", {"base_url": "https://ex.test"})
URL = "https://ex.test/example"
GOOD = "本文使用构造的研究样本说明风险分散方法，比较不同组合的表现，并提示结果依赖样本与假设。"


def add(conn: sqlite3.Connection, url: str = URL) -> None:
    store.upsert_discovered(conn, DiscoveredUrl(url, "ex", url.rsplit("/", 1)[-1]))
    store.replace_segments(
        conn,
        url,
        [
            Segment(0, "<p>Constructed research body.</p>"),
            Segment(1, "<p>Disclaimer appendix.</p>"),
        ],
    )
    store.set_status(conn, url, Status.EXTRACTED, title="Example", body_segments=1)


def backend(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    cap: int = 24000,
    model: str = "model-test",
) -> DeepSeekSummarizer:
    client = DeepSeekScorer(
        "mock-key",
        Rubric("summary-test", PROMPT),
        (),
        model=model,
        base_url="https://model.test",
        transport=httpx.MockTransport(handler),
        retries=0,
    )
    return DeepSeekSummarizer(client, cap)


def reply(text: str = GOOD, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "message": {"content": json.dumps({"summary": text}, ensure_ascii=False)},
                    "finish_reason": finish,
                }
            ]
        },
    )


def test_summary_cached_and_table_reads_matching_body(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        payload = json.loads(req.content)
        seen.append(payload)
        assert "Disclaimer appendix" not in payload["messages"][1]["content"]
        return reply()

    b = backend(handler)
    assert run(conn, SITE, b).generated == 1
    assert run(conn, SITE, b).reused == 1 and len(seen) == 1
    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.EXTRACTED
    assert valid_summary(conn, row) == GOOD
    assert records(conn, SITE, Glossary("1", {}))[0]["中文摘要"] == GOOD
    store.replace_segments(conn, URL, [Segment(0, "<p>Changed research.</p>")])
    assert records(conn, SITE, Glossary("1", {}))[0]["中文摘要"] == ""
    assert run(conn, SITE, b).generated == 1 and len(seen) == 2
    b.close()
    conn.close()


def test_model_input_limit_and_title_invalidate_cache(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append("call")
        return reply()

    first = backend(handler)
    assert run(conn, SITE, first).generated == 1
    other = backend(handler, model="model-other")
    assert run(conn, SITE, other).generated == 1
    capped = backend(handler, cap=10, model="model-other")
    st = run(conn, SITE, capped)
    assert st.generated == 1 and st.truncated == 1
    row = store.get_article(conn, URL)
    assert row is not None and row["summary_truncated"] == 1
    assert "摘要仅基于截取的正文" in records(conn, SITE, Glossary("1", {}))[0]["质量提示"]
    assert row["qc_issues"] is None
    store.set_fields(conn, URL, title="New title")
    assert run(conn, SITE, capped).generated == 1 and len(calls) == 4
    first.close()
    other.close()
    capped.close()
    conn.close()


@pytest.mark.parametrize(
    "text,finish",
    [
        ("", "stop"),
        ("An English only summary.", "stop"),
        (GOOD, "length"),
        (GOOD.rstrip("。"), "stop"),
    ],
)
def test_invalid_or_truncated_output_not_cached(tmp_path: Path, text: str, finish: str) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    b = backend(lambda req: reply(text, finish))
    stats = run(conn, SITE, b)
    assert stats.generated == 0 and stats.errors
    row = store.get_article(conn, URL)
    assert row is not None and row["summary_zh"] is None and row["summary_error"]
    assert row["status"] == Status.EXTRACTED and row["error"] is None
    b.close()
    conn.close()


@pytest.mark.parametrize("code,reason", [(402, "quota"), (429, "unavailable")])
def test_transport_stop_resumes_without_state_loss(tmp_path: Path, code: int, reason: str) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    add(conn, URL + "-2")
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append("call")
        return reply() if len(calls) == 1 else httpx.Response(code)

    b = backend(handler)
    st = run(conn, SITE, b)
    assert st.generated == 1 and st.stopped == reason and len(calls) == 2
    resumed = backend(lambda req: reply())
    st = run(conn, SITE, resumed)
    assert st.reused == 1 and st.generated == 1
    assert all(r["status"] == Status.EXTRACTED for r in store.site_rows(conn, "ex"))
    b.close()
    resumed.close()
    conn.close()


def test_selection_legacy_and_published_history(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    site = SiteConfig(
        "ex",
        "allowed",
        {**SITE.raw, "sections": {"list": [{"id": "all", "url_prefix": "/", "collect": False}]}},
    )
    b = backend(lambda req: reply())
    assert run(conn, site, b).generated == 0
    store.set_fields(conn, URL, section_key="url:", section="all")
    assert run(conn, site, b).generated == 0
    store.set_status(conn, URL, Status.PUBLISHED)
    assert run(conn, site, b).generated == 1
    b.close()
    conn.close()


def test_missing_key_fails_without_network(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    conn = store.connect(tmp_path / "db")
    add(conn)
    seen: list[str] = []
    b = DeepSeekSummarizer.from_env(
        env_file=None, transport=httpx.MockTransport(lambda req: seen.append("bad") or reply())
    )
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        run(conn, SITE, b)
    assert seen == []
    row = store.get_article(conn, URL)
    assert row is not None and row["status"] == Status.EXTRACTED
    b.close()
    conn.close()


def test_v3_schema_upgrades_without_erasing_articles(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    schema = (Path(__file__).resolve().parents[1] / "src/qbkb/store/schema.sql").read_text(
        encoding="utf-8"
    )
    schema = "\n".join(
        line
        for line in schema.splitlines()
        if not line.strip().startswith(("summary_", "fetched_at"))
    )
    conn.executescript(schema)
    conn.execute(
        "INSERT INTO articles (url,site,slug,status,updated_at)"
        " VALUES ('u','ex','s','published','t')"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()
    conn = store.connect(path)
    row = store.get_article(conn, "u")
    assert row is not None and row["status"] == Status.PUBLISHED and row["summary_zh"] is None
    assert row["fetched_at"] is None
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


def test_summarize_is_a_default_stage() -> None:
    assert "summarize" in cli.STAGES
    assert cli.build_parser().parse_args(["summarize"]).input_chars == 24000


def test_failed_regeneration_preserves_paid_summary(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    first = backend(lambda req: reply())
    assert run(conn, SITE, first).generated == 1
    other = backend(lambda req: reply("invalid English"), model="other-model")
    assert run(conn, SITE, other).errors
    row = store.get_article(conn, URL)
    assert row is not None and valid_summary(conn, row) == GOOD
    assert records(conn, SITE, Glossary("1", {}))[0]["中文摘要"] == GOOD
    first.close()
    other.close()
    conn.close()


@pytest.mark.parametrize("failure", ["missing-key", "quota"])
def test_transient_failure_preserves_previous_summary(tmp_path: Path, failure: str) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    good = backend(lambda req: reply())
    assert run(conn, SITE, good).generated == 1
    failing = backend(lambda req: httpx.Response(402), model="new-model")
    if failure == "missing-key":
        failing.client._api_key = ""
        with pytest.raises(RuntimeError, match="LLM_API_KEY"):
            run(conn, SITE, failing)
    else:
        assert run(conn, SITE, failing).stopped == "quota"
    row = store.get_article(conn, URL)
    assert row is not None and row["summary_error"] and valid_summary(conn, row) == GOOD
    good.close()
    failing.close()
    conn.close()


def test_english_dominant_output_rejected(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "db")
    add(conn)
    text = "中文测试摘要内容示例" + "English is the dominant content in this output." + "。"
    b = backend(lambda req: reply(text))
    assert run(conn, SITE, b).errors
    row = store.get_article(conn, URL)
    assert row is not None and row["summary_zh"] is None
    b.close()
    conn.close()
