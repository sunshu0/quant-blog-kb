"""DeepLTranslator 的离线测试：用假的 deepl 客户端，不访问网络。"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import deepl
import pytest

from qbkb import store
from qbkb.models import DiscoveredUrl, QuotaExceededError, Segment
from qbkb.translate import (
    DeepLTranslator,
    Glossary,
    SegmentTooLargeError,
    TranslatorUnavailableError,
    load_glossary,
    translate_article,
)
from qbkb.translate.deepl_backend import glossary_entries, glossary_name


class FakeClient:
    """记录调用；按 failures 列表依次抛异常，之后返回"译文"。"""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.translate_calls: list[dict[str, Any]] = []
        self.glossaries: list[deepl.GlossaryInfo] = []
        self.created: list[tuple[str, str, str, dict[str, str]]] = []
        self.deleted: list[str] = []

    def translate_text(self, text: Any, **kwargs: Any) -> Any:
        self.translate_calls.append({"text": list(text), **kwargs})
        if self.failures:
            raise self.failures.pop(0)
        return [deepl.TextResult(f"ZH:{t}", "EN", None) for t in text]

    def list_glossaries(self) -> list[deepl.GlossaryInfo]:
        return list(self.glossaries)

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo:
        self.created.append((name, source_lang, target_lang, entries))
        # id 按"建过几次"编号（删掉的也算），和 DeepL 一样不复用 id
        info = _info(f"id-{len(self.glossaries) + len(self.deleted)}", name, len(entries))
        self.glossaries.append(info)
        return info

    def delete_glossary(self, glossary: Any) -> None:
        self.deleted.append(str(glossary))
        self.glossaries = [g for g in self.glossaries if g.glossary_id != glossary]

    def get_usage(self) -> deepl.Usage:
        return deepl.Usage({"character_count": 1234, "character_limit": 500000})


def _info(gid: str, name: str, entries: int) -> deepl.GlossaryInfo:
    return deepl.GlossaryInfo(gid, name, True, "en", "zh", datetime.now(UTC), entries)


GLOSSARY = Glossary("1", {"drawdown": "回撤", "momentum": "动量"})


def _translator(client: FakeClient, **kwargs: Any) -> tuple[DeepLTranslator, list[float]]:
    sleeps: list[float] = []
    return DeepLTranslator(client, sleep=sleeps.append, **kwargs), sleeps


def test_request_parameters() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    out = t.translate(["<p>a</p>", "<p>b</p>"], GLOSSARY, "before")
    assert out == ["ZH:<p>a</p>", "ZH:<p>b</p>"]
    (call,) = client.translate_calls
    assert call["tag_handling"] == "html"
    assert call["source_lang"] == "EN"
    assert call["target_lang"] == "ZH-HANS"
    assert call["context"] == "before"
    assert call["glossary"] == "id-0"
    assert t.chars_sent == len("<p>a</p><p>b</p>")


def test_name_includes_target_language() -> None:
    assert _translator(FakeClient())[0].name == "deepl:ZH-HANS"
    assert _translator(FakeClient(), target_lang="ZH-HANT")[0].name == "deepl:ZH-HANT"


def test_tm_is_separate_per_target_language(tmp_path: Path) -> None:
    conn = store.connect(tmp_path / "state.db")
    url = "https://example.test/a"
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="example", slug="a"))
    store.replace_segments(conn, url, [Segment(idx=0, html="<p>Hello.</p>")])
    hans, hant = FakeClient(), FakeClient()
    translate_article(conn, url, _translator(hans)[0], GLOSSARY)
    translate_article(conn, url, _translator(hant, target_lang="ZH-HANT")[0], GLOSSARY)
    # 繁体没有命中简体的译文，自己翻了一次
    assert len(hans.translate_calls) == len(hant.translate_calls) == 1
    conn.close()


def test_no_context_sends_none() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    t.translate(["x"], {})
    assert client.translate_calls[0]["context"] is None
    assert client.translate_calls[0]["glossary"] is None
    assert client.created == []  # 空术语表不建 DeepL 术语表
    assert t.translate([], GLOSSARY) == []


def test_batches_respect_limits() -> None:
    client = FakeClient()
    t, _ = _translator(client, max_texts=2, max_chars=10)
    texts = ["aaaa", "bbbb", "cccc", "dddddddd", "e"]
    out = t.translate(texts, {})
    assert out == [f"ZH:{x}" for x in texts]
    assert [c["text"] for c in client.translate_calls] == [
        ["aaaa", "bbbb"],
        ["cccc"],
        ["dddddddd", "e"],
    ]
    assert t.requests == 3


def test_oversized_segment_fails_before_any_request() -> None:
    client = FakeClient()
    t, _ = _translator(client, max_chars=10)
    with pytest.raises(SegmentTooLargeError):
        t.translate(["ok", "x" * 11], GLOSSARY)
    assert client.translate_calls == [] and client.created == []


def test_glossary_uploads_case_variants() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    t.translate(["x"], GLOSSARY)
    ((name, src, tgt, entries),) = client.created
    assert (src, tgt) == ("EN", "ZH")
    assert entries == glossary_entries(GLOSSARY)
    assert entries["Drawdown"] == "回撤" and entries["drawdown"] == "回撤"
    assert name == glossary_name(GLOSSARY) and name.startswith("qbkb-v1-")


def test_glossary_created_once_and_reused() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    t.translate(["x"], GLOSSARY)
    t.translate(["y"], GLOSSARY)
    assert len(client.created) == 1
    # 新进程（新 translator）：DeepL 上已有同名术语表，直接复用
    t2, _ = _translator(client)
    assert t2.sync_glossary(GLOSSARY) == "id-0"
    assert len(client.created) == 1


def test_glossary_change_creates_new() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    t.sync_glossary(GLOSSARY)
    new = Glossary("2", {**GLOSSARY, "carry": "套息"})
    assert t.sync_glossary(new) == "id-1"
    assert [c[0] for c in client.created] == [glossary_name(GLOSSARY), glossary_name(new)]
    assert client.created[1][3]["Carry"] == "套息"


def test_glossary_with_wrong_entry_count_is_not_reused() -> None:
    client = FakeClient()
    client.glossaries.append(_info("stale", glossary_name(GLOSSARY), len(GLOSSARY)))
    t, _ = _translator(client)
    # 词条数是展开变体后的数量，旧的（没展开）不复用；同名的不是"旧版本"，不删
    assert t.sync_glossary(GLOSSARY) == "id-1"
    assert client.deleted == []


def test_glossary_upgrade_deletes_only_our_old_versions() -> None:
    """DeepL Free 同时只能保留 1 份术语表：换版本时先删 qbkb-v* 的旧表，别人的不动。"""
    client = FakeClient()
    client.glossaries += [
        _info("old-v1", "qbkb-v1-aaaaaaaa", 3),
        _info("old-v2", "qbkb-v2-bbbbbbbb", 3),
        _info("theirs", "my-own-glossary", 3),
        _info("plain", "qbkb-cccccccc", 3),  # 不带版本号的名字（测试用 Mapping 起的）不删
    ]
    t, _ = _translator(client)
    new = Glossary("3", {"drawdown": "回撤"})
    t.sync_glossary(new)
    assert client.deleted == ["old-v1", "old-v2"]
    assert t.deleted_glossaries == ["qbkb-v1-aaaaaaaa", "qbkb-v2-bbbbbbbb"]
    assert {g.name for g in client.glossaries} == {
        "my-own-glossary",
        "qbkb-cccccccc",
        glossary_name(new),
    }


@pytest.mark.parametrize(
    "name",
    [
        "qbkb-v1",  # 没有指纹
        "qbkb-v1-ABCDEF12",  # 大写：不是 glossary_name 生成的
        "qbkb-v1-aaaa-extra",
        "qbkb-vx-aaaaaaaa",
        "qbkb-v1-aaaaaaaa (copy)",
        "my-qbkb-v1-aaaaaaaa",
    ],
)
def test_names_that_only_look_like_ours_are_not_deleted(name: str) -> None:
    client = FakeClient()
    client.glossaries.append(_info("x", name, 3))
    t, _ = _translator(client)
    t.sync_glossary(Glossary("3", {"drawdown": "回撤"}))
    assert client.deleted == []


def test_old_glossary_already_gone_does_not_stop_translation() -> None:
    """删旧术语表时 404（别的进程 / 手工已经删了）：跳过，照常建新表、翻译。"""

    class Gone(FakeClient):
        def delete_glossary(self, glossary: Any) -> None:
            raise deepl.GlossaryNotFoundException("Glossary not found", http_status_code=404)

    client = Gone()
    client.glossaries.append(_info("old", "qbkb-v1-aaaaaaaa", 3))
    t, sleeps = _translator(client)
    assert t.translate(["x"], Glossary("3", {"drawdown": "回撤"})) == ["ZH:x"]
    assert t.deleted_glossaries == [] and len(client.created) == 1 and sleeps == []


def test_current_glossary_is_reused_without_deleting() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    gid = t.sync_glossary(GLOSSARY)
    t2, _ = _translator(client)
    assert t2.sync_glossary(GLOSSARY) == gid
    assert client.deleted == []


@pytest.mark.parametrize(
    "error",
    [
        # SDK 把 456 一律报成"额度用完"，消息里带着 DeepL 的说明
        deepl.QuotaExceededException(
            "Quota for this billing period has been exceeded, message: Too many glossaries",
            http_status_code=456,
        ),
        deepl.DeepLException("Bad request, message: Too many glossaries", http_status_code=400),
    ],
)
def test_too_many_glossaries_is_not_reported_as_quota(error: Exception) -> None:
    from qbkb.translate import GlossaryLimitError

    class Full(FakeClient):
        def create_glossary(
            self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
        ) -> deepl.GlossaryInfo:
            raise error

    t, sleeps = _translator(Full())
    with pytest.raises(GlossaryLimitError) as info:
        t.translate(["x"], GLOSSARY)
    assert not isinstance(info.value, QuotaExceededError)
    msg = str(info.value)
    assert "术语表数量到上限" in msg and "不是翻译额度用完" in msg and "删掉不用的术语表" in msg
    assert sleeps == []


@pytest.mark.usefixtures("man_without_sections")
def test_too_many_glossaries_exit_code_is_error_not_quota(tmp_path: Path) -> None:
    """CLI：术语表上限报成普通错误（退出码 1），提示里没有"额度用完、下个月再跑"。"""
    from qbkb import cli
    from qbkb.translate import GlossaryLimitError

    class Boom:
        name = "deepl:ZH-HANS"

        def translate(self, segments: Any, glossary: Any, context: str = "") -> list[str]:
            raise GlossaryLimitError("DeepL 账户里的术语表数量到上限了")

    db = tmp_path / "state.db"
    conn = store.connect(db)
    url = "https://example.test/a"
    store.upsert_discovered(conn, DiscoveredUrl(url=url, site="man", slug="a"))
    store.replace_segments(conn, url, [Segment(idx=0, html="<p>Hello drawdown.</p>")])
    from qbkb.models import Status

    store.set_status(conn, url, Status.FILTERED)
    conn.close()
    code = cli.main(
        ["translate", "--site", "man", "--db", str(db)], deps=cli.Deps(translator=Boom())
    )
    assert code == cli.EXIT_ERROR


def test_plain_mapping_glossary_named_by_fingerprint() -> None:
    assert glossary_name({"a": "甲"}).startswith("qbkb-")
    assert glossary_name({"a": "甲"}) != glossary_name({"a": "乙"})
    assert glossary_name(load_glossary()).startswith("qbkb-v3-")


def test_quota_exceeded_is_translated() -> None:
    client = FakeClient([deepl.QuotaExceededException("Quota exceeded", http_status_code=456)])
    t, sleeps = _translator(client)
    with pytest.raises(QuotaExceededError):
        t.translate(["x"], {})
    assert sleeps == []
    assert t.chars_sent == 0


@pytest.mark.parametrize(
    "error",
    [
        deepl.TooManyRequestsException("429", http_status_code=429),
        deepl.ConnectionException("timeout", should_retry=True),
        deepl.DeepLException("server error", http_status_code=503),
    ],
)
def test_transient_errors_back_off_then_succeed(error: Exception) -> None:
    client = FakeClient([error, error])
    t, sleeps = _translator(client, backoff_s=2.0)
    assert t.translate(["x"], {}) == ["ZH:x"]
    assert sleeps == [2.0, 4.0]
    assert len(client.translate_calls) == 3
    assert t.chars_sent == 1  # 只算成功的那次


def test_rate_limit_gives_up_with_project_error() -> None:
    errors: list[Exception] = [deepl.TooManyRequestsException("429") for _ in range(6)]
    t, sleeps = _translator(FakeClient(errors), retries=3, backoff_s=10.0, max_backoff_s=25.0)
    with pytest.raises(TranslatorUnavailableError):
        t.translate(["x"], {})
    assert sleeps == [10.0, 20.0, 25.0]


def test_default_backoff_is_bounded() -> None:
    errors: list[Exception] = [deepl.TooManyRequestsException("429") for _ in range(10)]
    t, sleeps = _translator(FakeClient(errors))
    with pytest.raises(TranslatorUnavailableError):
        t.translate(["x"], {})
    assert len(sleeps) == 5 and sum(sleeps) <= 70


@pytest.mark.parametrize(
    "error",
    [
        deepl.AuthorizationException("bad key", http_status_code=403),
        deepl.ConnectionException("bad url", should_retry=False),
        deepl.DeepLException("bad request", http_status_code=400),
    ],
)
def test_permanent_errors_are_not_retried(error: Exception) -> None:
    client = FakeClient([error])
    t, sleeps = _translator(client)
    with pytest.raises(type(error)):
        t.translate(["x"], {})
    assert sleeps == [] and len(client.translate_calls) == 1


def test_glossary_calls_are_retried_too() -> None:
    class FlakyList(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        def list_glossaries(self) -> list[deepl.GlossaryInfo]:
            if self.fail_once:
                self.fail_once = False
                raise deepl.TooManyRequestsException("429")
            return super().list_glossaries()

    t, sleeps = _translator(FlakyList())
    assert t.sync_glossary(GLOSSARY) == "id-0"
    assert sleeps == [2.0]


def test_wrong_result_count_is_an_error() -> None:
    class Short(FakeClient):
        def translate_text(self, text: Any, **kwargs: Any) -> Any:
            return super().translate_text(text, **kwargs)[:1]

    t, _ = _translator(Short())
    with pytest.raises(RuntimeError):
        t.translate(["a", "b"], {})


def test_usage() -> None:
    t, _ = _translator(FakeClient())
    assert t.usage() == (1234, 500000)


def test_from_env_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPL_API_KEY"):
        DeepLTranslator.from_env(env_file=None)


def test_from_env_turns_off_sdk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[str, dict[str, Any]]] = []

    def fake_client(key: str, **kwargs: Any) -> FakeClient:
        built.append((key, kwargs))
        return FakeClient()

    monkeypatch.setenv("DEEPL_API_KEY", "not-a-real-key:fx")
    monkeypatch.setattr(deepl, "DeepLClient", fake_client)
    t = DeepLTranslator.from_env(env_file=None)
    assert built == [("not-a-real-key:fx", {"max_retries": 0})]  # 只保留我们这一层重试
    assert t.name == "deepl:ZH-HANS"


def test_from_env_builds_real_client_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只构造客户端，不发请求
    monkeypatch.setenv("DEEPL_API_KEY", "not-a-real-key:fx")
    assert DeepLTranslator.from_env(env_file=None).name == "deepl:ZH-HANS"


@pytest.mark.network
def test_real_deepl_smoke() -> None:
    """真实冒烟：需要 .env 里的 DEEPL_API_KEY，消耗约 200 字符。verify.sh 不跑。"""
    try:
        t = DeepLTranslator.from_env()
    except RuntimeError:
        pytest.skip("没有 DEEPL_API_KEY")
    src = '<p><a href="https://example.test/x">Drawdown</a> is a fall from a peak.<br/>Next.</p>'
    (out,) = t.translate([src], load_glossary(), "Risk management basics.")
    assert "回撤" in out  # 句首大写的术语靠大小写变体命中
    assert 'href="https://example.test/x"' in out
    count, limit = t.usage()
    assert limit is not None and count is not None
