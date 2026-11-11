"""PR #8 审查：查额度不能卡住翻译。usage() 只问一次；CLI 等不到就跳过。"""

import threading
import time
from typing import Any

import deepl
import pytest

from qbkb import cli
from qbkb.translate import FREE_MONTHLY_CHARS, DeepLTranslator


class UsageClient:
    """假的 deepl 客户端：get_usage 按 failures 依次抛异常。"""

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.usage_calls = 0

    def translate_text(self, text: Any, **kwargs: Any) -> Any:
        raise AssertionError("这里不翻译")

    def list_glossaries(self) -> list[deepl.GlossaryInfo]:
        return []

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo:
        raise AssertionError("这里不建术语表")

    def get_usage(self) -> deepl.Usage:
        self.usage_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return deepl.Usage({"character_count": 7, "character_limit": 1_000_000})


def test_usage_does_not_back_off_and_retry() -> None:
    client = UsageClient([deepl.TooManyRequestsException("429"), deepl.DeepLException("boom")])
    sleeps: list[float] = []
    t = DeepLTranslator(client, sleep=sleeps.append)
    with pytest.raises(deepl.TooManyRequestsException):
        t.usage()
    assert (client.usage_calls, sleeps) == (1, [])  # 翻译请求遇到 429 会退避重试，查额度不会
    assert cli.translator_quota(t) == (None, FREE_MONTHLY_CHARS, False)
    assert cli.translator_quota(t) == (7, 1_000_000, True)
    assert sleeps == []


class StuckTranslator:
    """usage() 卡住不返回（直到 release 被 set）。"""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def usage(self) -> tuple[int, int]:
        self.started.set()
        self.release.wait(30)
        return 1, 2


def test_stuck_usage_is_skipped_after_a_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "USAGE_TIMEOUT_S", 0.05)
    t = StuckTranslator()
    try:
        start = time.monotonic()
        quota = cli.translator_quota(t)
        waited = time.monotonic() - start
        assert t.started.is_set()
        assert quota == (None, FREE_MONTHLY_CHARS, False)
        assert waited < 5  # 没有等 usage() 返回
        line = cli.quota_line(t, "翻译前")
        assert line is not None and "已用未知" in line and "默认值" in line
    finally:
        t.release.set()


def test_default_timeout_is_short() -> None:
    assert 0 < cli.USAGE_TIMEOUT_S <= 10
