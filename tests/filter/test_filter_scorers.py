"""打分后端：假后端、JSON 解析、OpenAI 兼容接口（httpx.MockTransport，不联网）。"""

import json
from collections.abc import Callable

import httpx
import pytest

from qbkb.filter import (
    DeepSeekScorer,
    FakeScorer,
    ScoreFormatError,
    ScorerUnavailableError,
    load_filter_config,
    load_rubric,
    parse_score,
)
from qbkb.models import QuotaExceededError

CATS = ("量化方法论", "基础概念", "宏观周评")
GOOD = {"score": 8, "category": "量化方法论", "evergreen": True, "reason": "讲清了方法"}


def test_fake_scorer_is_deterministic_and_counts_quota() -> None:
    s = FakeScorer("v1", quota_calls=2)
    assert s.version == "v1@fake"
    a = s.score("A Moment in Macro: rates", "")
    assert (a.category, a.value, a.evergreen) == ("宏观周评", 3.0, False)
    b = s.score("Building a carry signal", "nothing special here")
    assert (b.category, b.value) == ("量化方法论", 8.0)
    assert b.rubric_version == "v1@fake"
    with pytest.raises(QuotaExceededError):
        s.score("x", "y")
    assert len(s.calls) == 2


def test_fake_scorer_checks_title_before_excerpt() -> None:
    s = FakeScorer()
    # 标题命中"基础概念"，摘录里的 climate 不影响
    assert s.score("What is skew?", "climate").category == "基础概念"
    assert s.score("Untitled", "a climate note").category == "ESG/气候"


def test_parse_score_accepts_fenced_json() -> None:
    text = "```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
    s = parse_score(text, CATS, "v1@m")
    assert (s.value, s.category, s.evergreen, s.reason) == (8.0, "量化方法论", True, "讲清了方法")
    assert s.rubric_version == "v1@m"


@pytest.mark.parametrize(
    "patch",
    [
        {"score": 11},
        {"score": "8"},
        {"score": True},
        {"category": "体育"},
        {"evergreen": "yes"},
        {"reason": ""},
    ],
)
def test_parse_score_rejects_bad_fields(patch: dict[str, object]) -> None:
    with pytest.raises(ScoreFormatError):
        parse_score(json.dumps({**GOOD, **patch}), CATS, "v1")


def test_parse_score_rejects_non_json() -> None:
    with pytest.raises(ScoreFormatError, match="不是 JSON"):
        parse_score("分数 8", CATS, "v1")


Handler = Callable[[httpx.Request], httpx.Response]


def _reply(content: str) -> httpx.Response:
    body = {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 42}}
    return httpx.Response(200, json=body)


def _scorer(handler: Handler, sleeps: list[float] | None = None, **kw: object) -> DeepSeekScorer:
    cfg = load_filter_config()
    rubric = load_rubric(cfg.rubric_version, cfg)
    sink = sleeps if sleeps is not None else []
    return DeepSeekScorer(
        "sk-test",
        rubric,
        cfg.categories,
        base_url="https://llm.example.test/v1/",
        model="m1",
        transport=httpx.MockTransport(handler),
        sleep=sink.append,
        **kw,  # type: ignore[arg-type]
    )


def test_request_shape_and_success() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return _reply(json.dumps(GOOD, ensure_ascii=False))

    s = _scorer(handler)
    assert s.version == "v1@m1"
    score = s.score("A title", "first words")
    assert score.category == "量化方法论" and score.rubric_version == "v1@m1"
    (req,) = seen
    assert str(req.url) == "https://llm.example.test/v1/chat/completions"
    assert req.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["model"] == "m1" and body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert "A title" in body["messages"][1]["content"]
    assert "first words" in body["messages"][1]["content"]
    assert s.tokens == 42


def test_bad_json_is_asked_again_then_gives_up() -> None:
    replies = iter(["不是 JSON", json.dumps({**GOOD, "category": "体育"}), json.dumps(GOOD)])
    msgs: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        msgs.append(len(json.loads(req.content)["messages"]))
        return _reply(next(replies))

    assert _scorer(handler).score("t", "e").value == 8.0
    assert msgs == [2, 4, 6]  # 每次重问都带上前一次的输出和错误说明

    def always_bad(req: httpx.Request) -> httpx.Response:
        return _reply("nope")

    with pytest.raises(ScoreFormatError, match="重问 1 次"):
        _scorer(always_bad, parse_retries=1).score("t", "e")


def test_429_backs_off_with_retry_after_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, text="slow down")
        if calls["n"] == 2:
            return httpx.Response(503)
        return _reply(json.dumps(GOOD))

    sleeps: list[float] = []
    _scorer(handler, sleeps).score("t", "e")
    assert sleeps == [7.0, 4.0]  # 第一次照 Retry-After，第二次 2*2**1


def test_unavailable_after_retries() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=req)

    sleeps: list[float] = []
    with pytest.raises(ScorerUnavailableError):
        _scorer(handler, sleeps, retries=2).score("t", "e")
    assert sleeps == [2.0, 4.0]


@pytest.mark.parametrize(
    "resp",
    [
        httpx.Response(402, json={"error": {"message": "Insufficient Balance"}}),
        httpx.Response(429, json={"error": {"code": "insufficient_quota"}}),
    ],
)
def test_quota_exhausted(resp: httpx.Response) -> None:
    with pytest.raises(QuotaExceededError):
        _scorer(lambda req: resp).score("t", "e")


def test_bad_key_is_not_retried() -> None:
    sleeps: list[float] = []
    with pytest.raises(RuntimeError, match="401"):
        _scorer(lambda req: httpx.Response(401, text="bad key"), sleeps).score("t", "e")
    assert sleeps == []


def test_missing_key_fails_only_when_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODEL", "other-model")
    cfg = load_filter_config()
    s = DeepSeekScorer.from_env(load_rubric(cfg.rubric_version, cfg), cfg, env_file=None)
    assert s.version == "v1@other-model"  # 环境变量优先于 filters.yaml
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        s.score("t", "e")
