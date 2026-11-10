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
from qbkb.translate.base import MAX_TEXTS_PER_REQUEST, chunk_texts, count_chars
from qbkb.translate.glossary import Glossary, terms_fingerprint

GLOSSARY_PREFIX = "qbkb-"
# DeepL 请求体最多 128 KiB；字符上限留足余量（context 也算在请求体里）
MAX_CHARS_PER_REQUEST = 30_000


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
    ) -> Any: ...

    def list_glossaries(self) -> list[deepl.GlossaryInfo]: ...

    def create_glossary(
        self, name: str, source_lang: Any, target_lang: Any, entries: dict[str, str]
    ) -> deepl.GlossaryInfo: ...

    def get_usage(self) -> deepl.Usage: ...


def glossary_name(glossary: Mapping[str, str]) -> str:
    """DeepL 上术语表的名字。名字里带版本和内容指纹，内容不变就复用，变了才新建。"""
    if isinstance(glossary, Glossary):
        return f"{GLOSSARY_PREFIX}v{glossary.tm_version}"
    return f"{GLOSSARY_PREFIX}{terms_fingerprint(glossary)}"


class DeepLTranslator:
    """实现 models.Translator，另外支持 translate_with_context。

    限流：deepl SDK 自己会对 429 做几次短退避（约 15 秒内）；仍然 429 时，这里再按
    backoff_s、2×backoff_s……（不超过 max_backoff_s）等待，最多 rate_limit_retries 次。
    """

    def __init__(
        self,
        client: DeepLApi,
        *,
        source_lang: str = "EN",
        target_lang: str = "ZH-HANS",
        glossary_target_lang: str = "ZH",
        max_texts: int = MAX_TEXTS_PER_REQUEST,
        max_chars: int = MAX_CHARS_PER_REQUEST,
        rate_limit_retries: int = 3,
        backoff_s: float = 30.0,
        max_backoff_s: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.glossary_target_lang = glossary_target_lang
        self.max_texts = max_texts
        self.max_chars = max_chars
        self.rate_limit_retries = rate_limit_retries
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
        return cls(deepl.DeepLClient(key), **kwargs)

    @property
    def name(self) -> str:
        return "deepl"

    def translate(self, segments: Sequence[str], glossary: Mapping[str, str]) -> list[str]:
        return self.translate_with_context(segments, glossary, "")

    def translate_with_context(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str
    ) -> list[str]:
        if not segments:
            return []
        glossary_id = self.sync_glossary(glossary)
        out: list[str] = []
        for chunk in chunk_texts(segments, self.max_texts, self.max_chars):
            out.extend(self._request([segments[i] for i in chunk], glossary_id, context))
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
        target = self.glossary_target_lang.lower()
        found = next(
            (
                g
                for g in self._client.list_glossaries()
                if g.name == name
                and g.source_lang.lower() == self.source_lang.lower()
                and g.target_lang.lower() == target
                and g.entry_count == len(glossary)
            ),
            None,
        )
        if found is None:
            found = self._client.create_glossary(
                name, self.source_lang, self.glossary_target_lang, dict(glossary)
            )
        self._glossary_ids[name] = found.glossary_id
        return found.glossary_id

    def usage(self) -> tuple[int | None, int | None]:
        """本月已用字符数和上限（访问 DeepL，但不消耗额度）。"""
        chars = self._client.get_usage().character
        return chars.count, chars.limit

    def _request(self, texts: list[str], glossary_id: str | None, context: str) -> list[str]:
        attempt = 0
        while True:
            try:
                result = self._client.translate_text(
                    texts,
                    source_lang=self.source_lang,
                    target_lang=self.target_lang,
                    tag_handling="html",
                    context=context or None,
                    # 传 id 而不是 GlossaryInfo：SDK 对 GlossaryInfo 的语言比对区分大小写
                    glossary=glossary_id,
                )
                break
            except deepl.QuotaExceededException as e:
                raise QuotaExceededError(f"DeepL 额度用完：{e}") from e
            except deepl.TooManyRequestsException:
                if attempt >= self.rate_limit_retries:
                    raise
                self._sleep(min(self.backoff_s * 2**attempt, self.max_backoff_s))
                attempt += 1
        results = result if isinstance(result, list) else [result]
        if len(results) != len(texts):
            raise RuntimeError(f"DeepL 返回 {len(results)} 条译文，送出 {len(texts)} 条")
        self.requests += 1
        self.chars_sent += count_chars(texts)
        return [r.text for r in results]
