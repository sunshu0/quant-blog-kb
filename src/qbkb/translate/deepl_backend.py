"""DeepL API 后端（design.md 4.5）：HTML 模式、术语表同步、额度与限流处理、分批。"""

from __future__ import annotations

import os
import re
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
    ArticleTranslationError,
    BackendAuthError,
    TranslatorUnavailableError,
    chunk_texts,
    count_chars,
)
from qbkb.translate.footnotes import SUP_MARK, protect_sups, repair_marks, restore_sups
from qbkb.translate.glossary import Glossary, case_variants, terms_fingerprint
from qbkb.translate.lines import LineSplit, split_lines

GLOSSARY_PREFIX = "qbkb-"
# 带版本号的术语表名字的前缀（glossary_name）。换版本时只删这种名字的旧术语表：
# DeepL Free 账户同时只能保留 1 份术语表（2026-11-12 实测），不删旧的就建不了新的
# 只认 glossary_name 生成的形状 qbkb-v<版本>-<十六进制指纹>，别的名字（哪怕以 qbkb-v 开头）不碰
OUR_VERSIONED_NAME = re.compile(rf"^{re.escape(GLOSSARY_PREFIX)}v\d+-[0-9a-f]+$")


class GlossaryLimitError(RuntimeError):
    """DeepL 账户里的术语表数量到上限了（不是翻译额度用完）。

    DeepL 对这种情况也返回 456，SDK 报成 QuotaExceededException（"Quota ... exceeded,
    message: Too many glossaries"），不能按"额度用完、下个月再跑"处理。
    """


def _is_glossary_limit(e: deepl.DeepLException) -> bool:
    return "glossar" in str(e).lower()


GLOSSARY_LIMIT_HINT = (
    "DeepL 账户里的术语表数量到上限了（Free 账户同时只能保留 1 份），这不是翻译额度用完。"
    "qbkb 已经删掉了自己的旧版本术语表（名字以 qbkb-v 开头）；剩下的是别的程序或手工建的。"
    "建议：在 DeepL 账户里删掉不用的术语表，或者确认没有另一份 qbkb 正在同时运行，"
    "然后重跑同一条命令"
)
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

    # delete_glossary(glossary_id) 也会用到，但不放进协议：只翻译、不建术语表的测试替身不用实现它
    # （sync_glossary 用 getattr 取，没有就不删）

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
        self.deleted_glossaries: list[str] = []  # 这次删掉的旧术语表名字
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

    def request_units(
        self, html: str, glossary: Mapping[str, str] | None = None
    ) -> tuple[int, int]:
        """这一段会展开成几个翻译单元、总长多少（分行 + 脚注占位之后）。

        pipeline 用单元数和总长两个维度分批，保证一批正好对应后端的一个请求，不会因为
        单元数超限（脚注列表分行后一行一个单元）被后端再切成多个请求。
        glossary 对 DeepL 没有影响（术语表在服务端，不改变送出的长度），只为了和 Azure
        后端的 request_units 保持同一签名。
        """
        del glossary
        plan = split_lines(html)
        pieces = plan.lines if plan is not None else [html]
        lengths = [len(protect_sups(p)) for p in pieces]
        return len(lengths), sum(lengths)

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
        新建之前先删掉我们自己的旧版本术语表（名字精确匹配 qbkb-v<版本>-<指纹>、
        且不是当前这个名字）：
        DeepL Free 账户同时只能保留 1 份术语表。别人的术语表（其他名字）不动。
        术语表数量到上限时抛 GlossaryLimitError（不是额度用完）。
        """
        if not glossary:
            return None
        name = glossary_name(glossary)
        if name in self._glossary_ids:
            return self._glossary_ids[name]
        entries = glossary_entries(glossary)
        target = self.glossary_target_lang.lower()
        existing = self._with_retry(self._client.list_glossaries)
        found = next(
            (
                g
                for g in existing
                if g.name == name
                and g.source_lang.lower() == self.source_lang.lower()
                and g.target_lang.lower() == target
                and g.entry_count == len(entries)
            ),
            None,
        )
        delete: Callable[[str], object] | None = getattr(self._client, "delete_glossary", None)
        if found is None:
            for old in existing:
                if delete is None or old.name == name or not OUR_VERSIONED_NAME.match(old.name):
                    continue
                gid = old.glossary_id
                try:
                    self._with_retry(lambda gid=gid: delete(gid))
                except (deepl.GlossaryNotFoundException, deepl.NotFoundException):
                    continue  # 已经被删掉了（别的进程 / 手工）：不影响翻译
                self.deleted_glossaries.append(old.name)
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
                # 456 也用来表示"术语表太多"：那不是额度问题，下个月再跑也没用
                if _is_glossary_limit(e):
                    raise GlossaryLimitError(f"{GLOSSARY_LIMIT_HINT}（DeepL：{e}）") from e
                raise QuotaExceededError(f"DeepL 额度用完：{e}") from e
            except deepl.DeepLException as e:
                if "too many glossaries" in str(e).lower():
                    raise GlossaryLimitError(f"{GLOSSARY_LIMIT_HINT}（DeepL：{e}）") from e
                status = e.http_status_code
                if status in (401, 403):
                    # 术语表同步（list/create）的鉴权失败也要走同一条提示，别等 main() 兜底
                    raise BackendAuthError(
                        f"DeepL 鉴权失败（HTTP {status}），请检查 .env 里的 DEEPL_API_KEY"
                    ) from e
                if not _retryable(e):
                    raise
                if attempt >= self.retries:
                    raise TranslatorUnavailableError(
                        f"DeepL 暂时不可用，重试 {self.retries} 次后放弃：{e}"
                    ) from e
                self._sleep(min(self.backoff_s * 2**attempt, self.max_backoff_s))
                attempt += 1

    def _map_request_error(self, e: deepl.DeepLException) -> RuntimeError | None:
        """把翻译请求的不可重试错误归到"这篇的问题"还是"整个后端的问题"。"""
        status = e.http_status_code
        if status in (401, 403):
            return BackendAuthError(
                f"DeepL 鉴权失败（HTTP {status}），请检查 .env 里的 DEEPL_API_KEY"
            )
        if status is not None and 400 <= status < 500:
            return ArticleTranslationError(f"DeepL 拒绝这次请求（HTTP {status}）：{e}")
        return None

    def _request(self, texts: list[str], glossary_id: str | None, context: str) -> list[str]:
        try:
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
        except deepl.DeepLException as e:
            mapped = self._map_request_error(e)
            if mapped is not None:
                raise mapped from e
            raise
        results = result if isinstance(result, list) else [result]
        if len(results) != len(texts):
            raise ArticleTranslationError(f"DeepL 返回 {len(results)} 条译文，送出 {len(texts)} 条")
        self.requests += 1
        self.chars_sent += count_chars(texts)
        return [r.text for r in results]
