"""DeepLTranslator 的离线测试：用假的 deepl 客户端，不访问网络。"""

from datetime import UTC, datetime
from typing import Any

import deepl
import pytest

from qbkb.models import QuotaExceededError
from qbkb.translate import DeepLTranslator, Glossary, load_glossary
from qbkb.translate.deepl_backend import glossary_name


class FakeClient:
    """记录调用；按 failures 列表依次抛异常，之后返回"译文"。"""

    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = list(failures or [])
        self.translate_calls: list[dict[str, Any]] = []
        self.glossaries: list[deepl.GlossaryInfo] = []
        self.created: list[tuple[str, str, str, dict[str, str]]] = []

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
        info = _info(f"id-{len(self.glossaries)}", name, len(entries))
        self.glossaries.append(info)
        return info

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
    out = t.translate_with_context(["<p>a</p>", "<p>b</p>"], GLOSSARY, "before")
    assert out == ["ZH:<p>a</p>", "ZH:<p>b</p>"]
    (call,) = client.translate_calls
    assert call["tag_handling"] == "html"
    assert call["source_lang"] == "EN"
    assert call["target_lang"] == "ZH-HANS"
    assert call["context"] == "before"
    assert call["glossary"] == "id-0"
    assert t.chars_sent == len("<p>a</p><p>b</p>")
    assert t.name == "deepl"


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


def test_glossary_created_once_and_reused() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    t.translate(["x"], GLOSSARY)
    t.translate(["y"], GLOSSARY)
    assert client.created == [("qbkb-v" + GLOSSARY.tm_version, "EN", "ZH", dict(GLOSSARY))]
    # 新进程（新 translator）：DeepL 上已有同名术语表，直接复用
    t2, _ = _translator(client)
    assert t2.sync_glossary(GLOSSARY) == "id-0"
    assert len(client.created) == 1


def test_glossary_version_change_creates_new() -> None:
    client = FakeClient()
    t, _ = _translator(client)
    t.sync_glossary(GLOSSARY)
    new = Glossary("2", {**GLOSSARY, "carry": "套息"})
    assert t.sync_glossary(new) == "id-1"
    assert [c[0] for c in client.created] == [glossary_name(GLOSSARY), glossary_name(new)]
    assert client.created[1][3]["carry"] == "套息"


def test_glossary_with_wrong_entry_count_is_not_reused() -> None:
    client = FakeClient()
    client.glossaries.append(_info("stale", glossary_name(GLOSSARY), 1))
    t, _ = _translator(client)
    assert t.sync_glossary(GLOSSARY) == "id-1"


def test_plain_mapping_glossary_named_by_fingerprint() -> None:
    assert glossary_name({"a": "甲"}).startswith("qbkb-")
    assert glossary_name({"a": "甲"}) != glossary_name({"a": "乙"})
    assert glossary_name(load_glossary()).startswith("qbkb-v1-")


def test_quota_exceeded_is_translated() -> None:
    client = FakeClient([deepl.QuotaExceededException("Quota exceeded", http_status_code=456)])
    t, sleeps = _translator(client)
    with pytest.raises(QuotaExceededError):
        t.translate(["x"], {})
    assert sleeps == []
    assert t.chars_sent == 0


def test_rate_limit_backs_off_then_succeeds() -> None:
    too_many = [deepl.TooManyRequestsException("429", http_status_code=429) for _ in range(2)]
    client = FakeClient(list(too_many))
    t, sleeps = _translator(client, backoff_s=30.0)
    assert t.translate(["x"], {}) == ["ZH:x"]
    assert sleeps == [30.0, 60.0]
    assert len(client.translate_calls) == 3
    assert t.chars_sent == 1  # 只算成功的那次


def test_rate_limit_gives_up_after_retries() -> None:
    errors: list[Exception] = [deepl.TooManyRequestsException("429") for _ in range(5)]
    client = FakeClient(errors)
    t, sleeps = _translator(client, rate_limit_retries=3, backoff_s=100.0, max_backoff_s=250.0)
    with pytest.raises(deepl.TooManyRequestsException):
        t.translate(["x"], {})
    assert sleeps == [100.0, 200.0, 250.0]


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


def test_from_env_builds_free_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只构造客户端，不发请求
    monkeypatch.setenv("DEEPL_API_KEY", "not-a-real-key:fx")
    t = DeepLTranslator.from_env(env_file=None)
    assert t.name == "deepl"


@pytest.mark.network
def test_real_deepl_smoke() -> None:
    """真实冒烟：需要 .env 里的 DEEPL_API_KEY，消耗约 200 字符。verify.sh 不跑。"""
    try:
        t = DeepLTranslator.from_env()
    except RuntimeError:
        pytest.skip("没有 DEEPL_API_KEY")
    src = '<p>A <a href="https://example.test/x">drawdown</a> is a fall from a peak.</p>'
    (out,) = t.translate_with_context([src], load_glossary(), "Risk management basics.")
    assert "回撤" in out
    assert 'href="https://example.test/x"' in out
    count, limit = t.usage()
    assert limit is not None and count is not None
