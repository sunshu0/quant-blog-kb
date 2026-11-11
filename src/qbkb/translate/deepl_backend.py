"""DeepL API 后端（design.md 4.5）：HTML 模式、术语表同步、额度与限流处理、分批。"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import deepl
from dotenv import load_dotenv

from qbkb.models import QuotaExceededError
from qbkb.translate.base import (
    MAX_REQUEST_CHARS,
    MAX_TEXTS_PER_REQUEST,
    TranslatorUnavailableError,
    chunk_texts,
    count_chars,
)
from qbkb.translate.footnotes import SUP_MARK, protect_sups, repair_marks, restore_sups
from qbkb.translate.glossary import Glossary, case_variants, terms_fingerprint
from qbkb.translate.lines import LineSplit, split_lines

GLOSSARY_PREFIX = "qbkb-"
# 这些标签连同里面的内容原样保留、不送去翻译（DeepL 的 ignore_tags）。
# 光靠它保不住脚注上标（真实请求验证过，仍会丢标签），纯数字的上标另有占位链接
# （footnotes.py）；这里管剩下的上标（"20<sup>th</sup>" 之类），内容本来也不该翻译。
IGNORE_TAGS: tuple[str, ...] = ("sup",)


class DeepLApi(Protocol):
    """translate 用到的 deepl.DeepLClient 方法子集。测试里用假客户端实现它。"""

    def translate_text(
        self,
        text: Any,
        *,
        source_lang: Any = ...,
        target_lang: Any = ...,
        context: str | None = ...,
        glossary: Any = ...,
        tag_handling: Any = ...,
        ignore_tags: Any = ...,
    ) -> Any: ...

    def list_glossaries(self) -> list[deepl.GlossaryInfo]: ...

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo: ...

    def get_usage(self) -> deepl.Usage: ...


def glossary_entries(glossary: Mapping[str, str]) -> dict[str, str]:
    """真正上传到 DeepL 的词条：yaml 里的词条加上大小写变体。"""
    return case_variants(glossary)


def glossary_name(glossary: Mapping[str, str]) -> str:
    """DeepL 上术语表的名字：版本 + 上传词条的指纹。内容不变就复用，变了才新建。"""
    fp = terms_fingerprint(glossary_entries(glossary))
    if isinstance(glossary, Glossary):
        return f"{GLOSSARY_PREFIX}v{glossary.version}-{fp}"
    return f"{GLOSSARY_PREFIX}{fp}"


def _retryable(e: deepl.DeepLException) -> bool:
    if isinstance(e, deepl.TooManyRequestsException):
        return True
    if isinstance(e, deepl.ConnectionException):
        return e.should_retry
    return e.http_status_code is not None and e.http_status_code >= 500


class DeepLTranslator:
    """实现 models.Translator（支持 context 参数）。

    重试只有一层，就是这里：from_env 创建客户端时把 SDK 自带的重试关掉（max_retries=0）。
    遇到 429、可重试的网络错误、5xx 时依次等 backoff_s、2×backoff_s……（每次不超过
    max_backoff_s），最多 retries 次；默认 2+4+8+16+32 秒，最坏约 1 分钟（不含每次请求本身的超时）。
    重试用尽抛 TranslatorUnavailableError；额度用完抛 QuotaExceededError。
    """

    def __init__(
        self,
        client: DeepLApi,
        *,
        source_lang: str = "EN",
        target_lang: str = "ZH-HANS",
        glossary_target_lang: str = "ZH",
        ignore_tags: Sequence[str] = IGNORE_TAGS,
        max_texts: int = MAX_TEXTS_PER_REQUEST,
        max_chars: int = MAX_REQUEST_CHARS,
        retries: int = 5,
        backoff_s: float = 2.0,
        max_backoff_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.glossary_target_lang = glossary_target_lang
        self.ignore_tags = tuple(ignore_tags)
        self.max_texts = max_texts
        self.max_chars = max_chars
        self.retries = retries
        self.backoff_s = backoff_s
        self.max_backoff_s = max_backoff_s
        self._sleep = sleep
        self._glossary_ids: dict[str, str] = {}
        self.chars_sent = 0
        self.requests = 0

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env", **kwargs: Any) -> DeepLTranslator:
        """从环境变量 / .env 的 DEEPL_API_KEY 创建。

        Free key 以 ":fx" 结尾，SDK 会自动选 Free 端点。
        """
        if env_file is not None:
            load_dotenv(env_file)
        key = os.environ.get("DEEPL_API_KEY", "").strip()
        if not key:
            raise RuntimeError("没有找到 DEEPL_API_KEY：请在 .env 里设置")
        return cls(deepl.DeepLClient(key, max_retries=0), **kwargs)

    @property
    def name(self) -> str:
        """翻译记忆的 backend 键，带目标语言：换了目标语言不会命中别的语言的译文。"""
        return f"deepl:{self.target_lang}"

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]:
        if not segments:
            return []
        # 先按送出去的长度（脚注换成占位链接以后）检查：超长段落在发任何请求之前就报
        # SegmentTooLargeError。pipeline 用同一个长度（footnotes.sent_length）挑出 oversized
        chunk_texts([protect_sups(s) for s in segments], self.max_texts, self.max_chars)
        glossary_id = self.sync_glossary(glossary)
        # 分行排版的段落一行一个单元（lines.py）；脚注上标换成占位链接（footnotes.py）
        units: list[str] = []
        plans: list[tuple[LineSplit | None, int, int]] = []
        for seg in segments:
            plan = split_lines(seg)
            pieces = plan.lines if plan is not None else [seg]
            plans.append((plan, len(units), len(pieces)))
            units.extend(protect_sups(p) for p in pieces)
        translated: list[str] = []
        for chunk in chunk_texts(units, self.max_texts, self.max_chars):
            translated.extend(self._request([units[i] for i in chunk], glossary_id, context))
        out: list[str] = []
        for seg, (plan, start, n) in zip(segments, plans, strict=True):
            keep = SUP_MARK in seg
            parts = [restore_sups(t, keep_marks=keep) for t in translated[start : start + n]]
            tgt = plan.join(parts) if plan is not None else parts[0]
            out.append(repair_marks(seg, tgt))
        return out

    def sync_glossary(self, glossary: Mapping[str, str]) -> str | None:
        """确保 DeepL 上有和本地内容一致的术语表，返回它的 id。空术语表返回 None。

        按名字查找：已有同名、语言对一致、词条数一致的就复用，否则新建。
        旧版本的术语表不删除（DeepL 上的东西由用户自己清理）。
        """
        if not glossary:
            return None
        name = glossary_name(glossary)
        if name in self._glossary_ids:
            return self._glossary_ids[name]
        entries = glossary_entries(glossary)
        target = self.glossary_target_lang.lower()
        found = next(
            (
                g
                for g in self._with_retry(self._client.list_glossaries)
                if g.name == name
                and g.source_lang.lower() == self.source_lang.lower()
                and g.target_lang.lower() == target
                and g.entry_count == len(entries)
            ),
            None,
        )
        if found is None:
            found = self._with_retry(
                lambda: self._client.create_glossary(
                    name, self.source_lang, self.glossary_target_lang, entries
                )
            )
        self._glossary_ids[name] = found.glossary_id
        return found.glossary_id

    def usage(self) -> tuple[int | None, int | None]:
        """本月已用字符数和上限（访问 DeepL，但不消耗额度）。

        只问一次、不走退避重试：这个数字只用来显示，取不到就算了（调用方自己兜底），
        不能因为它卡住翻译。失败时抛 deepl.DeepLException。
        """
        chars = self._client.get_usage().character
        return chars.count, chars.limit

    def _with_retry[T](self, fn: Callable[[], T]) -> T:
        attempt = 0
        while True:
            try:
                return fn()
            except deepl.QuotaExceededException as e:
                raise QuotaExceededError(f"DeepL 额度用完：{e}") from e
            except deepl.DeepLException as e:
                if not _retryable(e):
                    raise
                if attempt >= self.retries:
                    raise TranslatorUnavailableError(
                        f"DeepL 暂时不可用，重试 {self.retries} 次后放弃：{e}"
                    ) from e
                self._sleep(min(self.backoff_s * 2**attempt, self.max_backoff_s))
                attempt += 1

    def _request(self, texts: list[str], glossary_id: str | None, context: str) -> list[str]:
        result = self._with_retry(
            lambda: self._client.translate_text(
                texts,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
                tag_handling="html",
                ignore_tags=list(self.ignore_tags) or None,
                context=context or None,
                # 传 id 而不是 GlossaryInfo：SDK 对 GlossaryInfo 的语言比对区分大小写
                glossary=glossary_id,
            )
        )
        results = result if isinstance(result, list) else [result]
        if len(results) != len(texts):
            raise RuntimeError(f"DeepL 返回 {len(results)} 条译文，送出 {len(texts)} 条")
        self.requests += 1
        self.chars_sent += count_chars(texts)
        return [r.text for r in results]
