"""微软 Azure AI Translator（Translator Text v3）后端：HTML 模式、动态词典术语、均匀限速、额度处理。

官方文档（2026-11-12 查看）：
- 认证：https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/reference/authentication
  只需要 Ocp-Apim-Subscription-Key；Ocp-Apim-Subscription-Region 对"区域资源 / 多服务资源"是必须的，
  对"全局资源"可选。所以 region 填了就发，没填就不发（401 时提示检查 region）。
- 单次请求上限：https://learn.microsoft.com/en-us/azure/ai-services/translator/service-limits
  Translate 每个请求最多 50,000 字符、1000 个元素。F0 每小时 200 万字符，要均匀使用
  （约每分钟 33,300 字符，滑动窗口），太快会收到额度错误。
- 错误码：https://learn.microsoft.com/en-us/azure/ai-services/translator/text-translation/reference/status-response-codes
  403001 = 免费额度用完；429000/429001/429002 = 超过请求限制；500/503 可重试。
- 响应头 X-metered-usage = 这次请求计费的字符数。Azure 没有 DeepL /v2/usage 那样的用量查询接口
  （只有 Azure Monitor 指标，要另外的管理凭据），所以用量由我们自己累计（metered_chars）。

术语表：Azure 没有服务端术语表。这里用请求里的动态词典标记
<mstrans:dictionary translation="回撤">drawdown</mstrans:dictionary>：
- 文档说它区分大小写、必须写 from、HTML 模式下照样能用；我们自己在本地做匹配（大小写不敏感，
  缩写区分大小写），把原文里实际出现的写法包起来，所以"区分大小写"不影响命中。
- 文档说它"只对专有名词安全"。中文没有词形变化，强制译名不会造成英文那种语法问题；
  复数（drawdowns）连同词尾一起包进去，避免"回撤s"。真实冒烟时要重点看这一项。
- 译后检查每个包进去的术语的译名是否出现在译文里，没出现的记在 glossary_misses，供人工复查。
"""

from __future__ import annotations

import html
import os
import re
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

from qbkb.models import QuotaExceededError
from qbkb.translate.base import (
    MAX_REQUEST_CHARS,
    TranslatorUnavailableError,
    chunk_texts,
)
from qbkb.translate.footnotes import SUP_MARK, protect_sups, repair_marks, restore_sups
from qbkb.translate.lines import LineSplit, split_lines

GLOBAL_ENDPOINT = "https://api.cognitive.microsofttranslator.com"
API_VERSION = "3.0"
# Azure Translate 的单次请求上限（service limits）
AZURE_MAX_ELEMENTS = 1000
AZURE_MAX_REQUEST_CHARS = 50_000
# F0：每小时 200 万字符、要均匀使用 → 每分钟约 33,300。默认留一点余量
F0_CHARS_PER_HOUR = 2_000_000
DEFAULT_CHARS_PER_MINUTE = 30_000
# F0 每月免费额度（定价页）。只用于提示，Azure 没有接口能查到真实的已用量
F0_MONTHLY_CHARS = 2_000_000

# 送去翻译时内容原样保留的标签（相当于 DeepL 的 ignore_tags）：加 class="notranslate"，译后去掉。
# 纯数字的脚注上标另有占位链接（footnotes.py），这里管剩下的上标（"20<sup>th</sup>"）
NOTRANSLATE_TAGS: tuple[str, ...] = ("sup",)
# 这些标签里的文字不做术语匹配（代码、上标、本来就不翻译的内容）
_NO_TERM_TAGS = frozenset({"code", "pre", "kbd", "samp", "script", "style", "sup", "mstrans"})

_TAG = re.compile(r"(<[^>]*>)")
_TAG_NAME = re.compile(r"^<\s*(/?)\s*([A-Za-z][\w:-]*)")
_DICT_LEFTOVER = re.compile(
    r"<mstrans:dictionary\b[^>]*?\btranslation=([\"'])(.*?)\1[^>]*>.*?</mstrans:dictionary\s*>",
    re.IGNORECASE | re.DOTALL,
)
_DICT_STRAY = re.compile(r"</?mstrans:dictionary\b[^>]*>", re.IGNORECASE)
_ACRONYM = re.compile(r"^[A-Z][A-Z0-9/&]{1,5}$")


class AzureAuthError(RuntimeError):
    """key 无效、region 不对或没有权限（401 / 403000）。重试没用，要改 .env。"""


class AzureRequestError(RuntimeError):
    """请求本身有问题（400 之类），重试没用。"""


@dataclass(frozen=True)
class AzureError:
    status: int
    code: int | None
    message: str


def parse_error(response: httpx.Response) -> AzureError:
    """读出 {"error": {"code": 403001, "message": "..."}}；读不出来时 code 为 None。"""
    code: int | None = None
    message = response.text[:200]
    try:
        data: Any = response.json()
        err = data.get("error", {}) if isinstance(data, dict) else {}
        if isinstance(err, dict):
            raw = err.get("code")
            code = int(raw) if raw is not None else None
            message = str(err.get("message", message))
    except (ValueError, TypeError):
        pass
    return AzureError(response.status_code, code, message)


class CharRateLimiter:
    """滑动窗口限速：任意 window_s 秒内送出的字符不超过 limit。

    Azure F0 要求每小时 200 万字符均匀使用（文档按"每分钟约 33,300"解释），
    所以默认窗口 60 秒、上限 DEFAULT_CHARS_PER_MINUTE。单次请求超过 limit 时不拆，
    等窗口空了再整批送出（窗口里只剩它自己）。
    """

    def __init__(
        self,
        limit: int,
        window_s: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if limit < 1 or window_s <= 0:
            raise ValueError("limit 和 window_s 必须是正数")
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[tuple[float, int]] = deque()
        self.waited_s = 0.0  # 累计因为限速等了多久（显示 / 测试用）

    def _expire(self, now: float) -> None:
        while self._sent and self._sent[0][0] <= now - self.window_s:
            self._sent.popleft()

    def acquire(self, n: int) -> None:
        """送出 n 个字符之前调用：必要时等到窗口里放得下。"""
        while True:
            now = self._clock()
            self._expire(now)
            used = sum(c for _, c in self._sent)
            if not self._sent or used + n <= self.limit:
                self._sent.append((now, n))
                return
            # 等最早的一笔滑出窗口
            wait = self._sent[0][0] + self.window_s - now
            wait = max(wait, 0.01)
            self.waited_s += wait
            self._sleep(wait)


def _term_regex(src: str) -> re.Pattern[str]:
    """术语在正文里的写法：缩写区分大小写，其余不区分；左右都要词边界，允许复数词尾。"""
    plural = r"(?:s|es)?"
    if _ACRONYM.match(src):
        return re.compile(rf"(?<![\w/]){re.escape(src)}s?(?![\w/])")
    return re.compile(rf"(?<!\w){re.escape(src)}{plural}(?!\w)", re.IGNORECASE)


class TermMarker:
    """把正文里的术语包成动态词典标记。最长的术语优先，已包进去的地方不再匹配。"""

    def __init__(self, glossary: Mapping[str, str]) -> None:
        self.terms = dict(glossary)
        self._patterns = [
            (src, _term_regex(src)) for src in sorted(self.terms, key=len, reverse=True)
        ]

    def mark(self, html_text: str) -> tuple[str, list[str]]:
        """返回 (加了标记的 HTML, 包进去的术语原文列表，按出现次数)。"""
        if not self.terms:
            return html_text, []
        parts = _TAG.split(html_text)
        skip: list[str] = []  # 当前所在的、不做匹配的标签栈
        hits: list[str] = []
        out: list[str] = []
        for i, part in enumerate(parts):
            if i % 2:  # 标签
                m = _TAG_NAME.match(part)
                if m:
                    closing, name = m.group(1) == "/", m.group(2).lower()
                    base = name.split(":")[0]
                    no_term = base in _NO_TERM_TAGS or "notranslate" in part.lower()
                    if closing:
                        if skip and skip[-1] == name:
                            skip.pop()
                    elif no_term and not part.rstrip().endswith("/>"):
                        skip.append(name)
                out.append(part)
            elif skip or not part.strip():
                out.append(part)
            else:
                text, found = self._mark_text(part)
                hits.extend(found)
                out.append(text)
        return "".join(out), hits

    def _mark_text(self, text: str) -> tuple[str, list[str]]:
        spans: list[tuple[int, int, str]] = []
        for src, pat in self._patterns:
            for m in pat.finditer(text):
                s, e = m.span()
                if any(not (e <= a or s >= b) for a, b, _ in spans):
                    continue
                spans.append((s, e, src))
        if not spans:
            return text, []
        spans.sort()
        out: list[str] = []
        pos = 0
        for s, e, src in spans:
            zh = self.terms[src]
            # 原文和译文相同的词条（alpha、CTA）是"保留原文"：按原文里的写法保留大小写
            # （"Alpha Architect" 不能变成 "alpha Architect"）
            if zh.lower() == src.lower():
                zh = text[s:e]
            tgt = html.escape(zh, quote=True)
            out.append(text[pos:s])
            out.append(f'<mstrans:dictionary translation="{tgt}">{text[s:e]}</mstrans:dictionary>')
            pos = e
        out.append(text[pos:])
        return "".join(out), [src for _, _, src in spans]


def strip_dictionary_markup(text: str) -> str:
    """译文里如果还留着动态词典标记（正常情况下 Azure 会去掉），换成标记里的译名。"""
    text = _DICT_LEFTOVER.sub(lambda m: m.group(2), text)
    return _DICT_STRAY.sub("", text)


def add_notranslate(text: str, tags: Sequence[str]) -> str:
    """<sup> → <sup class="notranslate">（只处理没有属性的开始标签，往返无损）。"""
    for tag in tags:
        text = re.sub(rf"<{tag}>", f'<{tag} class="notranslate">', text)
    return text


def remove_notranslate(text: str, tags: Sequence[str]) -> str:
    for tag in tags:
        text = re.sub(rf'<{tag}\s+class="notranslate"\s*>', f"<{tag}>", text)
    return text


class AzureTranslator:
    """实现 models.Translator。context 参数 Azure 不支持，忽略。

    - 分批：每个请求不超过 max_elements 个元素、max_chars 个字符（按加了标记后的长度切批）；
    - 限速：每次请求前 limiter.acquire(字符数)，默认每分钟 30,000（F0 要求均匀使用）；
    - 429 / 5xx / 408 / 网络错误：退避重试（有 Retry-After 就按它等），
      用尽抛 TranslatorUnavailableError；
    - 403001（免费额度用完）：文档说"短时间送太多"也可能收到额度错误，所以先等 quota_cooldown_s
      再试一次，还是 403001 就抛 QuotaExceededError（流程停下保留进度，下次接着跑）；
    - 401 / 403000：AzureAuthError；其他 4xx：AzureRequestError。
    - metered_chars：按响应头 X-metered-usage 累计的实际计费字符（取不到时按送出的长度算）。
    """

    def __init__(
        self,
        client: httpx.Client,
        key: str,
        *,
        region: str = "",
        endpoint: str = GLOBAL_ENDPOINT,
        source_lang: str = "en",
        target_lang: str = "zh-Hans",
        notranslate_tags: Sequence[str] = NOTRANSLATE_TAGS,
        max_elements: int = AZURE_MAX_ELEMENTS,
        max_chars: int = MAX_REQUEST_CHARS,
        chars_per_minute: int = DEFAULT_CHARS_PER_MINUTE,
        limiter: CharRateLimiter | None = None,
        retries: int = 5,
        backoff_s: float = 2.0,
        max_backoff_s: float = 60.0,
        quota_cooldown_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_chars > AZURE_MAX_REQUEST_CHARS or max_elements > AZURE_MAX_ELEMENTS:
            raise ValueError("max_chars / max_elements 超过了 Azure 的单次请求上限")
        self._client = client
        self._key = key
        self.region = region
        self.endpoint = endpoint.rstrip("/")
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.notranslate_tags = tuple(notranslate_tags)
        self.max_elements = max_elements
        self.max_chars = max_chars
        self.limiter = limiter or CharRateLimiter(chars_per_minute, sleep=sleep)
        self.retries = retries
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self.quota_cooldown_s = quota_cooldown_s
        self._sleep = sleep
        self._markers: dict[int, TermMarker] = {}
        self.chars_sent = 0  # 送出去的字符（含标签和词典标记）
        self.metered_chars = 0  # Azure 实际计费的字符（X-metered-usage 累计）
        self.requests = 0
        self.term_hits = 0  # 包进动态词典的术语出现次数
        self.glossary_misses: list[tuple[str, str]] = []  # (术语, 译名) 译文里没出现的

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", **kwargs: Any) -> AzureTranslator:
        """从环境变量 / .env 创建：AZURE_TRANSLATOR_KEY（必填）、AZURE_TRANSLATOR_REGION、
        AZURE_TRANSLATOR_ENDPOINT（默认全局终结点）、AZURE_TRANSLATOR_CHARS_PER_MINUTE（限速）。"""
        if env_file is not None:
            load_dotenv(env_file)
        key = os.environ.get("AZURE_TRANSLATOR_KEY", "").strip()
        if not key:
            raise RuntimeError("没有找到 AZURE_TRANSLATOR_KEY：请在 .env 里设置")
        region = os.environ.get("AZURE_TRANSLATOR_REGION", "").strip()
        endpoint = os.environ.get("AZURE_TRANSLATOR_ENDPOINT", "").strip() or GLOBAL_ENDPOINT
        rate = os.environ.get("AZURE_TRANSLATOR_CHARS_PER_MINUTE", "").strip()
        if rate and "chars_per_minute" not in kwargs and "limiter" not in kwargs:
            try:
                kwargs["chars_per_minute"] = int(rate)
            except ValueError as e:
                raise RuntimeError("AZURE_TRANSLATOR_CHARS_PER_MINUTE 必须是整数") from e
        client = httpx.Client(timeout=30.0)
        return cls(client, key, region=region, endpoint=endpoint, **kwargs)

    @property
    def name(self) -> str:
        """翻译记忆的 backend 键，带目标语言；和 DeepL 的记忆（deepl:ZH-HANS）分开。"""
        return f"azure:{self.target_lang}"

    def close(self) -> None:
        self._client.close()

    def _marker(self, glossary: Mapping[str, str]) -> TermMarker:
        key = id(glossary)
        marker = self._markers.get(key)
        if marker is None or marker.terms != dict(glossary):
            marker = TermMarker(glossary)
            self._markers[key] = marker
        return marker

    def prepare(self, unit: str, glossary: Mapping[str, str]) -> tuple[str, list[str]]:
        """一个翻译单元送出去之前的样子：脚注占位 → 不翻译标签 → 术语标记。"""
        sent = add_notranslate(protect_sups(unit), self.notranslate_tags)
        return self._marker(glossary).mark(sent)

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        del context  # Azure 没有 context 参数
        if not segments:
            return []
        units: list[str] = []
        hits: list[list[str]] = []
        plans: list[tuple[LineSplit | None, int, int]] = []
        for seg in segments:
            plan = split_lines(seg)
            pieces = plan.lines if plan is not None else [seg]
            plans.append((plan, len(units), len(pieces)))
            for p in pieces:
                sent, found = self.prepare(p, glossary)
                units.append(sent)
                hits.append(found)
        # 先切批：超长段落在发任何请求之前就报 SegmentTooLargeError
        chunks = chunk_texts(units, self.max_elements, self.max_chars)
        translated: list[str] = []
        for chunk in chunks:
            translated.extend(self._request([units[i] for i in chunk]))
        cleaned: list[str] = []
        for i, t in enumerate(translated):
            t = remove_notranslate(strip_dictionary_markup(t), self.notranslate_tags)
            self._check_terms(hits[i], glossary, t)
            cleaned.append(t)
        out: list[str] = []
        for seg, (plan, start, n) in zip(segments, plans, strict=True):
            keep = SUP_MARK in seg
            parts = [restore_sups(t, keep_marks=keep) for t in cleaned[start : start + n]]
            tgt = plan.join(parts) if plan is not None else parts[0]
            out.append(repair_marks(seg, tgt))
        return out

    def _check_terms(self, found: list[str], glossary: Mapping[str, str], tgt: str) -> None:
        self.term_hits += len(found)
        for src in dict.fromkeys(found):
            zh = glossary[src]
            if zh.lower() not in tgt.lower():
                self.glossary_misses.append((src, zh))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Ocp-Apim-Subscription-Key": self._key,
            "Content-Type": "application/json; charset=UTF-8",
        }
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        return headers

    def _params(self) -> dict[str, str]:
        return {
            "api-version": API_VERSION,
            "from": self.source_lang,  # 动态词典要求必须写 from，不能自动检测
            "to": self.target_lang,
            "textType": "html",
        }

    def _backoff(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None:
            after = response.headers.get("Retry-After", "").strip()
            if after.isdigit():
                return min(float(after), self.max_backoff_s)
        return min(self.backoff_s * 2**attempt, self.max_backoff_s)

    def _request(self, texts: list[str]) -> list[str]:
        size = sum(len(t) for t in texts)
        body = [{"Text": t} for t in texts]
        attempt = 0
        quota_retried = False
        while True:
            self.limiter.acquire(size)
            try:
                r = self._client.post(
                    f"{self.endpoint}/translate",
                    params=self._params(),
                    headers=self._headers(),
                    json=body,
                )
            except httpx.TransportError as e:
                # httpx 的错误信息里只有地址，没有请求头，不会带出 key
                if attempt >= self.retries:
                    raise TranslatorUnavailableError(
                        f"Azure 连不上，重试 {self.retries} 次后放弃：{type(e).__name__}"
                    ) from e
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue
            if r.status_code == 200:
                return self._parse(r, texts, size)
            err = parse_error(r)
            if err.code == 403001:
                if quota_retried:
                    raise QuotaExceededError(
                        "Azure 免费额度用完（403001）：可能是本月 200 万字符用完，"
                        "也可能是短时间内送得太快（每小时 200 万）。过一小时再跑；"
                        f"还是这样就是本月额度用完。Azure：{err.message}"
                    )
                quota_retried = True
                self._sleep(self.quota_cooldown_s)
                continue
            if r.status_code == 401 or err.code == 403000:
                hint = (
                    ""
                    if self.region
                    else "；资源不是全局（Global）的话要设置 AZURE_TRANSLATOR_REGION"
                )
                raise AzureAuthError(
                    f"Azure 拒绝了 key（HTTP {r.status_code}，{err.code}）{hint}：{err.message}"
                )
            if r.status_code in (408, 429) or r.status_code >= 500:
                if attempt >= self.retries:
                    raise TranslatorUnavailableError(
                        f"Azure 暂时不可用（HTTP {r.status_code}，{err.code}），"
                        f"重试 {self.retries} 次后放弃：{err.message}"
                    )
                self._sleep(self._backoff(attempt, r))
                attempt += 1
                continue
            raise AzureRequestError(
                f"Azure 请求出错（HTTP {r.status_code}，{err.code}）：{err.message}"
            )

    def _parse(self, r: httpx.Response, texts: list[str], size: int) -> list[str]:
        try:
            data: Any = r.json()
            results = [str(item["translations"][0]["text"]) for item in data]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise AzureRequestError("Azure 的返回看不懂（不是 translate 的格式）") from e
        if len(results) != len(texts):
            raise RuntimeError(f"Azure 返回 {len(results)} 条译文，送出 {len(texts)} 条")
        metered = r.headers.get("X-metered-usage", "").strip()
        self.metered_chars += int(metered) if metered.isdigit() else size
        self.requests += 1
        self.chars_sent += size
        return results


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    message: str
    metered_chars: int = 0  # 这次检查花掉的计费字符


def check_connection(
    client: httpx.Client,
    key: str = "",
    *,
    region: str = "",
    endpoint: str = GLOBAL_ENDPOINT,
) -> ConnectionCheck:
    """测试连接。

    - 不给 key：GET /languages（不需要 key、不计费），只能证明网络通；
    - 给了 key：翻译 "OK" 两个字符（按 X-metered-usage 计费约 2 字符），能验证 key 和 region。
      Azure 没有不计费又需要 key 的接口，这是代价最小的验证方式。
    """
    base = endpoint.rstrip("/")
    try:
        if not key:
            r = client.get(
                f"{base}/languages", params={"api-version": API_VERSION, "scope": "translation"}
            )
            if r.status_code != 200:
                return ConnectionCheck(False, f"Azure 返回 HTTP {r.status_code}")
            return ConnectionCheck(True, "网络正常（没有验证 key）")
        headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/json; charset=UTF-8",
        }
        if region:
            headers["Ocp-Apim-Subscription-Region"] = region
        r = client.post(
            f"{base}/translate",
            params={"api-version": API_VERSION, "from": "en", "to": "zh-Hans"},
            headers=headers,
            json=[{"Text": "OK"}],
        )
    except httpx.HTTPError as e:
        return ConnectionCheck(False, f"连不上：{type(e).__name__}（检查网络或代理）")
    if r.status_code == 200:
        metered = r.headers.get("X-metered-usage", "").strip()
        used = int(metered) if metered.isdigit() else 2
        return ConnectionCheck(True, f"连接正常：key 有效（这次检查计费 {used} 字符）", used)
    err = parse_error(r)
    if err.code == 403001:
        return ConnectionCheck(False, "key 有效，但免费额度用完了（403001）")
    if r.status_code in (401, 403):
        hint = "" if region else "；资源不是全局（Global）的话要填 region"
        return ConnectionCheck(False, f"key 无效或没有权限（HTTP {r.status_code}）{hint}")
    return ConnectionCheck(False, f"Azure 返回 HTTP {r.status_code}（{err.code}）")
