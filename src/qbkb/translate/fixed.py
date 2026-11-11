"""不送 DeepL 的段落：人工校对过的固定译文，以及播客的说话人标签。

- **固定译文**（`config/fixed_translations.yaml`）：文末的"重要信息"和版权声明每篇都一样，
  人工校对一份，按原文段落的 hash（models.segment_hash）对应。命中时直接用，不送 DeepL、不计费；
  术语表改了也不会让它们重翻。文件不存在就没有固定译文。
- **说话人标签**：访谈稿里 "<p><b>Ann Smith:</b></p>" 这样单独成段的人名，原样保留英文。
  逐段翻译时同一个人会有好几种译名（验收发现一人三译），而人名本来也不必翻。
  只在访谈类文章（抽取的 tags 里有 PODCAST / VIDEO 等）里启用；名字要像人名（1–3 个首字母
  大写的词，不是 Pros、Example 这类常见小标题词）；同一个名字在一篇里出现两次以上，
  这篇里这个名字的所有标签都原样保留（"Ann Smith:" 和 "Ann Smith :" 处理一致）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import yaml

from qbkb.models import Segment

FIXED_FILE = "fixed_translations.yaml"
_HASH = re.compile(r"^[0-9a-f]{64}$")

_LABEL = re.compile(r"^<p><(b|strong)>\s*([^<]{1,60}?)\s*</\1>\s*</p>$")
# 1–3 个首字母大写的词，后面跟冒号："Ann Smith:"、"Ann Smith :"
_NAME = re.compile(r"^([A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,2})\s*[:：]$")
# 同一个名字在一篇里至少出现这么多次才算说话人标签
LABEL_MIN_REPEATS = 2
# 这些 tags 的文章是访谈稿（抽取阶段从 "PODCAST | 40 MIN" 这样的标签行取出）
INTERVIEW_TAGS = frozenset({"PODCAST", "VIDEO", "WEBINAR", "INTERVIEW"})
# 常见的小标题词：这些词组成的 "Pros:"、"Key Takeaways:" 不是人名
HEADING_WORDS = frozenset(
    {
        "advantages",
        "agenda",
        "answer",
        "answers",
        "background",
        "benefits",
        "case",
        "chart",
        "cons",
        "conclusion",
        "conclusions",
        "definition",
        "definitions",
        "disadvantages",
        "disclaimer",
        "example",
        "examples",
        "exhibit",
        "figure",
        "highlights",
        "host",
        "important",
        "introduction",
        "key",
        "lesson",
        "lessons",
        "moderator",
        "note",
        "notes",
        "outlook",
        "part",
        "points",
        "pros",
        "q",
        "a",
        "question",
        "questions",
        "risk",
        "risks",
        "source",
        "sources",
        "speaker",
        "step",
        "summary",
        "table",
        "takeaway",
        "takeaways",
        "the",
        "tip",
        "tips",
        "update",
        "warning",
    }
)


class FixedTranslations(Mapping[str, str]):
    """原文段落 hash → 中文 HTML。"""

    def __init__(self, entries: Mapping[str, str] | None = None) -> None:
        self._entries = dict(entries or {})

    def __getitem__(self, key: str) -> str:
        return self._entries[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"FixedTranslations({len(self)} 条)"


def load_fixed(path: Path) -> FixedTranslations:
    """读固定译文。格式：

    ```yaml
    entries:
      <64 位 hash>:
        note: 说明（可选，只给人看）
        zh: <p>中文 HTML</p>
    ```
    """
    if not path.exists():
        return FixedTranslations()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是映射")
    raw = data.get("entries") or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: entries 必须是映射")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not _HASH.match(key):
            raise ValueError(f"{path}: {key!r} 不是段落 hash（64 位十六进制）")
        zh = value.get("zh") if isinstance(value, dict) else None
        if not isinstance(zh, str) or not zh.strip():
            raise ValueError(f"{path}: {key[:12]}… 缺少 zh 译文")
        out[key] = zh.strip()
    return FixedTranslations(out)


def label_name(html: str) -> str | None:
    """像说话人标签（加粗的人名加冒号、单独成段）就返回规范化的名字，否则 None。"""
    m = _LABEL.match(html)
    if m is None:
        return None
    n = _NAME.match(m.group(2))
    if n is None:
        return None
    words = n.group(1).split()
    if any(w.lower().strip(".'’-") in HEADING_WORDS for w in words):
        return None
    return " ".join(words)


def is_label_shape(html: str) -> bool:
    """形状上像说话人标签。"""
    return label_name(html) is not None


def is_interview(tags: str | None) -> bool:
    """articles.tags（JSON 数组字符串）里有访谈类标签。"""
    if not tags:
        return False
    try:
        values = json.loads(tags)
    except ValueError:
        return False
    return isinstance(values, list) and any(
        str(v).strip().upper() in INTERVIEW_TAGS for v in values
    )


def speaker_labels(segments: Sequence[Segment], *, interview: bool = True) -> set[str]:
    """一篇文章里的说话人标签（段落 hash）。不是访谈稿时一个也没有。

    按规范化的名字计数：同一个名字出现 LABEL_MIN_REPEATS 次以上，这个名字的每个标签段落都算，
    同一篇里同一个人的标签处理一致。
    """
    if not interview:
        return set()
    names = {s.src_hash: label_name(s.html) for s in segments}
    counts = Counter(names[s.src_hash] for s in segments if names[s.src_hash])
    return {h for h, n in names.items() if n and counts[n] >= LABEL_MIN_REPEATS}


def untranslated_units(
    segments: Sequence[Segment], fixed: Mapping[str, str], *, interview: bool = False
) -> dict[str, tuple[str, str]]:
    """这篇文章里不送翻译的段落：hash → (译文, 来源 "fixed" / "speaker")。

    interview：这篇是不是访谈稿（is_interview(articles.tags)），只有访谈稿才认说话人标签。
    """
    out: dict[str, tuple[str, str]] = {}
    labels = speaker_labels(segments, interview=interview)
    for seg in segments:
        h = seg.src_hash
        if h in fixed:
            out[h] = (fixed[h], "fixed")
        elif h in labels:
            out[h] = (seg.html, "speaker")
    return out
