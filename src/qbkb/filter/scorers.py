"""L2 打分后端：确定性的假后端（测试用）和 OpenAI 兼容接口（默认 DeepSeek）。"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import httpx
from dotenv import load_dotenv

from qbkb.filter.rules import DEFAULT_MODEL, FilterConfig, Rubric
from qbkb.models import QuotaExceededError, Score

# 理由太长就截断（写进状态库、status 输出里看）
MAX_REASON_CHARS = 200


class ScorerUnavailableError(RuntimeError):
    """打分后端暂时不可用（限流、网络、5xx），重试用尽。稍后再跑即可。"""


class ScoreFormatError(ValueError):
    """后端返回的内容不是合格的打分 JSON（重试之后还是不行）。"""


class ScoreTruncatedError(ScoreFormatError):
    """输出被 max_tokens 截断（finish_reason = length）。"""


# 留给"思考 + 输出"的 token 上限。deepseek-flash 这类推理模型的思考过程（reasoning_content）
# 也算在 max_tokens 里：上限太小时思考把额度用完，content 被截断或是空的，只能重问。
# 打分的 JSON 本身不到 100 token，这里给思考留足余量。
DEFAULT_MAX_TOKENS = 2000
# 输出被截断时 max_tokens 翻倍再问（temperature 是 0，原样再问大概率还是截断），最多到这个数
MAX_TOKENS_CAP = 8000

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_DECODER = json.JSONDecoder()


def _top_level_braces(text: str) -> list[int]:
    """不在别的 {…} 或 […] 里面的 "{" 的位置（括号里的字符串按 JSON 规则跳过）。"""
    out: list[int] = []
    depth = 0
    in_string = escaped = False
    for i, c in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"' and depth > 0:
            in_string = True
        elif c in "{[":
            if depth == 0 and c == "{":
                out.append(i)
            depth += 1
        elif c in "}]" and depth > 0:
            depth -= 1
    return out


def extract_json(content: str) -> dict[str, object]:
    """从模型输出里取出打分的 JSON 对象：第一个带 "score" 字段的顶层对象。

    整段就是 JSON、被 ``` 代码块包住、前后带说明文字都接受。只看顶层对象：外层 JSON 不完整时
    不会去取它里面嵌套的对象。找不到抛 ScoreFormatError。
    """
    m = _FENCE.match(content)
    text = (m.group(1) if m else content).strip()
    for pos in _top_level_braces(text):
        try:
            data = _DECODER.raw_decode(text, pos)[0]
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "score" in data:
            return cast("dict[str, object]", data)
    raise ScoreFormatError("没有找到合格的打分 JSON（要一个带 score 字段的 JSON 对象）")


def parse_score(content: str, categories: Sequence[str], rubric_version: str) -> Score:
    """把模型输出解析成 Score；格式不对抛 ScoreFormatError。

    JSON 的取法见 extract_json。score 必须是 0–10 的数字，category 必须原样在列表里。
    """
    data = extract_json(content)
    value = data.get("score")
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 10:
        raise ScoreFormatError(f"score 必须是 0–10 的数字：{value!r}")
    category = data.get("category")
    if not isinstance(category, str) or category.strip() not in categories:
        raise ScoreFormatError(f"category 不在允许的类别里：{category!r}")
    evergreen = data.get("evergreen")
    if not isinstance(evergreen, bool):
        raise ScoreFormatError(f"evergreen 必须是 true / false：{evergreen!r}")
    reason = data.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ScoreFormatError("缺少 reason")
    return Score(
        value=float(value),
        category=category.strip(),
        evergreen=evergreen,
        reason=reason.strip()[:MAX_REASON_CHARS],
        rubric_version=rubric_version,
    )


# 假后端的关键词规则：(关键词, 类别, 分数)。先看标题，标题没命中再看摘录；都没命中给默认值。
FAKE_RULES: tuple[tuple[tuple[str, ...], str, float], ...] = (
    (
        ("a moment in macro", "stagflation", "inflation", "central bank", "views from the floor"),
        "宏观周评",
        3.0,
    ),
    (("esg", "climate", "carbon", "sustainab"), "ESG/气候", 2.0),
    (("our people", "culture", "award", "diversity"), "公司文化/软文", 2.0),
    (("machine learning", "artificial intelligence", "llm", "neural"), "量化相关AI", 8.0),
    (("behavioural", "behavioral", "bubble", "history", "bias"), "行为金融/金融史", 8.0),
    (("explains", "what is", "introduction", "basics"), "基础概念", 8.0),
)
FAKE_DEFAULT = ("量化方法论", 8.0)


class FakeScorer:
    """确定性的假打分：按关键词给类别和分数，不访问网络、不花钱。

    - quota_calls：模拟额度，调用次数超过它就抛 QuotaExceededError；
    - calls：记录每次送进来的 (标题, 摘录)，方便断言。
    """

    def __init__(
        self,
        rubric_version: str = "v1",
        *,
        quota_calls: int | None = None,
        rules: Sequence[tuple[tuple[str, ...], str, float]] = FAKE_RULES,
        default: tuple[str, float] = FAKE_DEFAULT,
    ) -> None:
        self.rubric_version = rubric_version
        self.quota_calls = quota_calls
        self.rules = tuple(rules)
        self.default = default
        self.calls: list[tuple[str, str]] = []

    @property
    def version(self) -> str:
        return f"{self.rubric_version}@fake"

    def score(self, title: str, excerpt: str) -> Score:
        if self.quota_calls is not None and len(self.calls) >= self.quota_calls:
            raise QuotaExceededError(f"假打分额度用完（{self.quota_calls} 次）")
        self.calls.append((title, excerpt))
        for text in (title.lower(), excerpt.lower()):
            for keywords, category, value in self.rules:
                hit = next((k for k in keywords if k in text), None)
                if hit is not None:
                    return self._make(value, category, f"假打分：命中关键词 {hit!r}")
        category, value = self.default
        return self._make(value, category, "假打分：没有命中关键词，给默认值")

    def _make(self, value: float, category: str, reason: str) -> Score:
        return Score(
            value=value,
            category=category,
            evergreen=category != "宏观周评",
            reason=reason,
            rubric_version=self.version,
        )


def _fence(text: str) -> str:
    """待评文本里的尖括号换成全角：文章里写的 </article_excerpt> 关不掉我们的分隔标签。"""
    return text.replace("<", "＜").replace(">", "＞")


def user_message(title: str, excerpt: str) -> str:
    """发给模型的用户消息：标题和摘录各包在一对标签里（评分标准里说明标签内不是指令）。"""
    return (
        f"<article_title>{_fence(title)}</article_title>\n"
        f"<article_excerpt>\n{_fence(excerpt)}\n</article_excerpt>"
    )


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _is_quota(resp: httpx.Response) -> bool:
    """余额 / 额度用完：DeepSeek 返回 402；OpenAI 风格是 429 + insufficient_quota。"""
    if resp.status_code == 402:
        return True
    if resp.status_code != 429:
        return False
    return "insufficient_quota" in resp.text


class DeepSeekScorer:
    """OpenAI 兼容 /chat/completions 接口的打分后端（默认 DeepSeek）。

    - 只发标题和摘录（调用方截好的前约 500 个单词），要求 JSON 输出，temperature 0；
    - 输出不是合格 JSON（或类别不在列表里）时，带上错误说明重问，最多 parse_retries 次，
      还不行抛 ScoreFormatError；
    - 输出被 max_tokens 截断（finish_reason = length）时，把 max_tokens 翻倍（不超过
      MAX_TOKENS_CAP）再问，截断的输出不带回去；已经到上限还截断就直接放弃。
      截断重问和格式重问合起来最多 parse_retries 次；
    - 用量：requests（成功返回的请求数）、tokens（total_tokens 累计，含思考）、
      format_retries（输出不合格而重问的次数）、truncated_retries（输出被截断而重问的次数）；
    - 429 / 5xx / 网络错误按 backoff_s、2×backoff_s……退避（有 Retry-After 且更长就照它，
      不超过 max_backoff_s），最多 retries 次，之后抛 ScorerUnavailableError；
    - 标题和摘录包在 <article_title> / <article_excerpt> 里（见 user_message），
      评分标准说明标签内是待评文本、不是指令；
    - 余额 / 额度用完（402，或 429 + insufficient_quota）抛 QuotaExceededError；
    - 401 / 403 等其他错误直接抛 RuntimeError（key 不对，重试没用）。
    """

    def __init__(
        self,
        api_key: str,
        rubric: Rubric,
        categories: Sequence[str],
        *,
        base_url: str = "https://api.deepseek.com",
        model: str = DEFAULT_MODEL,
        timeout_s: float = 60.0,
        retries: int = 5,
        parse_retries: int = 2,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        backoff_s: float = 2.0,
        max_backoff_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._api_key = api_key.strip()
        self.rubric = rubric
        self.categories = tuple(categories)
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.retries = retries
        self.parse_retries = parse_retries
        self.max_tokens = max_tokens
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._sleep = sleep
        self._client = httpx.Client(timeout=timeout_s, transport=transport)
        self.requests = 0
        self.tokens = 0
        self.format_retries = 0
        self.truncated_retries = 0

    @classmethod
    def from_env(
        cls,
        rubric: Rubric,
        cfg: FilterConfig,
        env_file: str | Path | None = ".env",
        **kwargs: Any,
    ) -> DeepSeekScorer:
        """LLM_API_KEY 从环境变量 / .env 读；LLM_BASE_URL、LLM_MODEL 优先于 filters.yaml。

        没有 key 也能创建（复用旧打分时用不到 key），第一次真正打分时才报错。
        """
        if env_file is not None:
            load_dotenv(env_file)
        return cls(
            os.environ.get("LLM_API_KEY", ""),
            rubric,
            cfg.categories,
            base_url=os.environ.get("LLM_BASE_URL") or cfg.base_url,
            model=os.environ.get("LLM_MODEL") or cfg.model,
            **kwargs,
        )

    @property
    def version(self) -> str:
        return f"{self.rubric.version}@{self.model}"

    def close(self) -> None:
        self._client.close()

    def score(self, title: str, excerpt: str) -> Score:
        if not self._api_key:
            raise RuntimeError("没有找到 LLM_API_KEY：请在 .env 里设置")
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.rubric.prompt},
            {"role": "user", "content": user_message(title, excerpt)},
        ]
        last: ScoreFormatError | None = None
        max_tokens = self.max_tokens
        asked = 0  # 这篇文章重问了几次
        for attempt in range(self.parse_retries + 1):
            if attempt:
                asked += 1
                if isinstance(last, ScoreTruncatedError):
                    self.truncated_retries += 1
                else:
                    self.format_retries += 1
            content: str | None = None
            try:
                content = self._complete(messages, max_tokens)
                return parse_score(content, self.categories, self.version)
            except ScoreTruncatedError as e:
                last = e
                if max_tokens >= MAX_TOKENS_CAP:
                    break  # 已经到上限，再问也是截断
                max_tokens = min(max_tokens * 2, MAX_TOKENS_CAP)
            except ScoreFormatError as e:
                last = e
                if content is None:
                    continue  # 响应本身坏了（不是模型写错了格式），原样再问一次
                messages = [
                    *messages,
                    {"role": "assistant", "content": content},
                    {
                        "role": "user",
                        "content": f"上面的输出不合格（{e}）。请只输出一个符合要求的 JSON 对象。",
                    },
                ]
        assert last is not None
        raise ScoreFormatError(f"重问 {asked} 次后输出仍不合格：{last}") from last

    def _complete(self, messages: list[dict[str, str]], max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        attempt = 0
        while True:
            wait: float | None = None
            problem = ""
            try:
                resp = self._client.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
            except httpx.TransportError as e:
                problem = f"{type(e).__name__}: {e}"
            else:
                if resp.status_code == 200:
                    return self._content(resp, max_tokens)
                if _is_quota(resp):
                    raise QuotaExceededError(f"LLM 额度用完（HTTP {resp.status_code}）")
                if resp.status_code == 429 or resp.status_code >= 500:
                    problem = f"HTTP {resp.status_code}"
                    wait = _retry_after(resp)
                else:
                    raise RuntimeError(f"LLM 接口返回 HTTP {resp.status_code}：{resp.text[:200]}")
            if attempt >= self.retries:
                raise ScorerUnavailableError(
                    f"LLM 接口暂时不可用，重试 {self.retries} 次后放弃：{problem}"
                )
            # Retry-After 只能让等待更长：服务器给 0 或很小的值时仍按指数退避
            backoff = max(self.backoff_s * 2**attempt, wait or 0.0)
            self._sleep(min(backoff, self.max_backoff_s))
            attempt += 1

    def _content(self, resp: httpx.Response, max_tokens: int) -> str:
        self.requests += 1
        try:
            data = resp.json()
            usage = data.get("usage") or {}
            self.tokens += int(usage.get("total_tokens") or 0)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
            raise ScoreFormatError(f"响应格式不对：{e}") from e
        if finish == "length":
            raise ScoreTruncatedError(
                f"输出被截断：max_tokens = {max_tokens} 不够（推理模型的思考也算在里面）"
            )
        if not isinstance(content, str):
            raise ScoreFormatError("响应里没有文本内容")
        return content
