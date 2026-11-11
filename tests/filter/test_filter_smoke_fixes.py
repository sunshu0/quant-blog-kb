"""真实冒烟后的打分修正：默认模型、推理模型的 max_tokens、JSON 提取、重问计数。"""

import json
from collections.abc import Callable

import httpx
import pytest

from qbkb.filter import DeepSeekScorer, ScoreFormatError, load_filter_config, load_rubric
from qbkb.filter.rules import DEFAULT_MODEL
from qbkb.filter.scorers import DEFAULT_MAX_TOKENS, extract_json, parse_score

CATS = ("量化方法论", "基础概念", "宏观周评")
GOOD = {"score": 8, "category": "量化方法论", "evergreen": True, "reason": "讲清了方法"}
GOOD_JSON = json.dumps(GOOD, ensure_ascii=False)

Handler = Callable[[httpx.Request], httpx.Response]


def _reply(content: str | None, finish: str = "stop", tokens: int = 100) -> httpx.Response:
    body = {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": content, "reasoning_content": "thinking..."},
            }
        ],
        "usage": {"total_tokens": tokens, "completion_tokens_details": {"reasoning_tokens": 50}},
    }
    return httpx.Response(200, json=body)


def _scorer(handler: Handler, **kw: object) -> DeepSeekScorer:
    cfg = load_filter_config()
    rubric = load_rubric(cfg.rubric_version, cfg)
    sleeps: list[float] = []
    return DeepSeekScorer(
        "sk-test",
        rubric,
        cfg.categories,
        base_url="https://llm.example.test/v1",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        **kw,  # type: ignore[arg-type]
    )


def test_default_model_is_deepseek_flash_everywhere() -> None:
    cfg = load_filter_config()
    assert DEFAULT_MODEL == "deepseek-flash" and cfg.model == "deepseek-flash"
    s = _scorer(lambda req: _reply(GOOD_JSON))
    # 复用旧打分靠这个版本串：评分标准版本 + 模型
    assert s.model == "deepseek-flash" and s.version == f"{cfg.rubric_version}@deepseek-flash"
    assert s.score("t", "e").rubric_version == s.version


def test_from_env_model_override_changes_version(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_filter_config()
    rubric = load_rubric(cfg.rubric_version, cfg)
    monkeypatch.setenv("LLM_MODEL", "other-model")
    s = DeepSeekScorer.from_env(rubric, cfg, env_file=None)
    assert s.version == f"{cfg.rubric_version}@other-model"
    monkeypatch.delenv("LLM_MODEL")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    assert DeepSeekScorer.from_env(rubric, cfg, env_file=None).model == "deepseek-flash"


@pytest.mark.parametrize(
    "content",
    [
        GOOD_JSON,
        f"```json\n{GOOD_JSON}\n```",
        f"```\n{GOOD_JSON}\n```",
        f"好的，打分如下：\n{GOOD_JSON}\n以上是我的判断。",
        f"说明 {{不是 json}} 然后 {GOOD_JSON}",
        f"下面是结果：\n```json\n{GOOD_JSON}\n```\n希望有帮助",
        f"{GOOD_JSON}\n{GOOD_JSON}",  # 输出了两遍：取第一个
    ],
)
def test_extract_json_variants(content: str) -> None:
    assert extract_json(content) == GOOD
    assert parse_score(content, CATS, "v1").value == 8.0


@pytest.mark.parametrize("content", ["", "   ", "分数 8", '{"score": 8, "category": "量化'])
def test_extract_json_rejects(content: str) -> None:
    with pytest.raises(ScoreFormatError, match="不是 JSON"):
        extract_json(content)


def test_max_tokens_leaves_room_for_reasoning() -> None:
    seen: list[dict[str, object]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return _reply(GOOD_JSON)

    s = _scorer(handler)
    s.score("t", "e")
    assert DEFAULT_MAX_TOKENS >= 1000 and seen[0]["max_tokens"] == DEFAULT_MAX_TOKENS
    assert (s.requests, s.tokens, s.format_retries) == (1, 100, 0)


def test_truncated_output_is_retried_without_feeding_it_back() -> None:
    # 推理模型把 max_tokens 用在思考上：content 是空的 / 半截，finish_reason = length
    replies = iter([_reply("", "length"), _reply('{"score": 8, "cat', "length"), _reply(GOOD_JSON)])
    msgs: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        msgs.append(len(json.loads(req.content)["messages"]))
        return next(replies)

    s = _scorer(handler)
    assert s.score("t", "e").value == 8.0
    assert msgs == [2, 2, 2]  # 截断的输出不带回去，原样再问
    assert (s.requests, s.tokens, s.format_retries) == (3, 300, 2)


def test_truncated_every_time_gives_a_clear_error() -> None:
    s = _scorer(lambda req: _reply("", "length"), parse_retries=1)
    with pytest.raises(ScoreFormatError, match="截断"):
        s.score("t", "e")
    assert (s.requests, s.format_retries) == (2, 1)


def test_format_retries_counts_bad_json() -> None:
    replies = iter(["不是 JSON", GOOD_JSON])
    s = _scorer(lambda req: _reply(next(replies)))
    s.score("t", "e")
    assert (s.requests, s.format_retries) == (2, 1)
