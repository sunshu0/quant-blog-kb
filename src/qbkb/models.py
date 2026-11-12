"""所有模块共用的数据类型和接口。

改这个文件会影响每一条线，所以只在单独的 PR 里改，不要夹在功能 PR 里。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class Status(StrEnum):
    """一篇文章在流程里的状态（见 design.md 第 2、5 节）。

    v2 默认流程：discovered → fetched → extracted → translated → published。
    filtered / skipped 只由可选的 `qbkb filter`（加工阶段）产生，不再是必经状态；
    unselected 是"不在采集的栏目里"：保留记录、不翻译，栏目改成采集后自动回到流程。
    """

    DISCOVERED = "discovered"
    FETCHED = "fetched"
    EXTRACTED = "extracted"
    UNSELECTED = "unselected"  # 未选栏目（v2）
    FILTERED = "filtered"
    SKIPPED = "skipped"
    TRANSLATED = "translated"
    NEEDS_REVIEW = "needs_review"  # 翻译有问题（结构校验没过、段落超长），等人工复查
    PUBLISHED = "published"
    FAILED = "failed"
    PDF_PENDING = "pdf_pending"


@dataclass(frozen=True, slots=True)
class DiscoveredUrl:
    """discover 的输出：一个待处理的文章 URL。"""

    url: str
    site: str
    slug: str
    lastmod: str | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """fetch 的输出。not_modified=True 表示对方返回 304，没有新内容。"""

    url: str
    http_status: int
    raw_path: str | None
    etag: str | None = None
    not_modified: bool = False


@dataclass(frozen=True, slots=True)
class ArticleMeta:
    """从 JSON-LD / meta 标签取得的元数据。"""

    title: str
    author: str | None = None
    published: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Segment:
    """正文的一个块级片段（清洗后的 HTML），是翻译和翻译记忆的最小单位。"""

    idx: int
    html: str

    @property
    def src_hash(self) -> str:
        return segment_hash(self.html)


@dataclass(frozen=True, slots=True)
class ExtractedArticle:
    """extract 的输出。"""

    url: str
    meta: ArticleMeta
    segments: tuple[Segment, ...] = field(default_factory=tuple)

    @property
    def body_hash(self) -> str:
        return body_hash(self.segments)


@dataclass(frozen=True, slots=True)
class Score:
    """filter L2 打分结果。"""

    value: float
    category: str
    evergreen: bool
    reason: str
    rubric_version: str


class QuotaExceededError(Exception):
    """翻译或打分后端的额度用完。流程应停下并保留进度，下次接着跑。"""


class Translator(Protocol):
    """可插拔翻译后端（DeepL、Azure、假后端……）。

    输入输出都是 HTML 片段，长度和顺序一一对应。
    context 是只作参考、不翻译的上下文（纯文本，比如前后段落）；不支持的后端忽略它。
    name 用作翻译记忆的 backend 键，应包含目标语言（例如 "deepl:ZH-HANS"）。
    """

    @property
    def name(self) -> str: ...

    def translate(
        self, segments: Sequence[str], glossary: Mapping[str, str], context: str = ""
    ) -> list[str]: ...


class Scorer(Protocol):
    """可插拔打分后端（便宜的 LLM API、假后端……）。

    version 是打分结果的版本标识（评分标准版本 + 后端，例如 "v1@deepseek:deepseek-chat"），
    写进 articles.rubric_version，也就是 score() 返回的 Score.rubric_version。
    正文没变（body_hash == scored_body_hash）且 version 相同时，filter 复用旧打分、不再调用后端。
    额度用完抛 QuotaExceededError。
    """

    @property
    def version(self) -> str: ...

    def score(self, title: str, excerpt: str) -> Score: ...


_WS = re.compile(r"\s+")
_PRE = re.compile(r"(<pre\b.*?</pre>)", re.IGNORECASE | re.DOTALL)


def normalize_html(html: str) -> str:
    """折叠空白，让只差空格换行的片段得到同一个 hash。

    <pre> 里的空白是内容（代码缩进），原样保留。
    """
    parts = _PRE.split(html)
    # split 带捕获组：偶数下标是 <pre> 之外的文本，奇数下标是 <pre> 块本身
    out = [p if i % 2 else _WS.sub(" ", p) for i, p in enumerate(parts)]
    return "".join(out).strip()


def segment_hash(html: str) -> str:
    return hashlib.sha256(normalize_html(html).encode("utf-8")).hexdigest()


def body_hash(segments: Sequence[Segment]) -> str:
    """整篇正文的指纹：按顺序拼接各段 hash 再算一次。段落顺序变了也算变化。"""
    joined = "\n".join(s.src_hash for s in segments)
    return hashlib.sha256(joined.encode("ascii")).hexdigest()
