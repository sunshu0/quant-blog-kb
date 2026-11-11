"""不送 DeepL 的段落：人工校对过的固定译文，以及播客的说话人标签。

- **固定译文**（`config/fixed_translations.yaml`）：文末的"重要信息"和版权声明每篇都一样，
  人工校对一份，按原文段落的 hash（models.segment_hash）对应。命中时直接用，不送 DeepL、不计费；
  术语表改了也不会让它们重翻。文件不存在就没有固定译文。
- **说话人标签**：访谈稿里 "<p><b>Ann Smith:</b></p>" 这样单独成段的人名，原样保留英文。
  逐段翻译时同一个人会有好几种译名（验收发现一人三译），而人名本来也不必翻。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import yaml

from qbkb.models import Segment

FIXED_FILE = "fixed_translations.yaml"
_HASH = re.compile(r"^[0-9a-f]{64}$")

_LABEL = re.compile(r"^<p><(b|strong)>\s*([^<]{1,60}?)\s*</\1>\s*</p>$")
# 1–4 个首字母大写的词，后面跟冒号："Ann Smith:"、"Ann Smith :"
_NAME = re.compile(r"^[A-Z][\w.'’-]*(?:\s+[A-Z][\w.'’-]*){0,3}\s*[:：]$")
# 同一个标签在一篇里至少出现这么多次才算说话人标签（"<p><b>Key Takeaways:</b></p>" 只出现一次）
LABEL_MIN_REPEATS = 2


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


def is_label_shape(html: str) -> bool:
    """形状上像说话人标签：一个加粗的短人名加冒号，单独成段。"""
    m = _LABEL.match(html)
    return m is not None and _NAME.match(m.group(2)) is not None


def speaker_labels(segments: Sequence[Segment]) -> set[str]:
    """一篇文章里的说话人标签（段落 hash）：形状对、而且同一段重复出现。"""
    counts = Counter(s.src_hash for s in segments)
    return {
        s.src_hash
        for s in segments
        if counts[s.src_hash] >= LABEL_MIN_REPEATS and is_label_shape(s.html)
    }


def untranslated_units(
    segments: Sequence[Segment], fixed: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """这篇文章里不送翻译的段落：hash → (译文, 来源 "fixed" / "speaker")。"""
    out: dict[str, tuple[str, str]] = {}
    labels = speaker_labels(segments)
    for seg in segments:
        h = seg.src_hash
        if h in fixed:
            out[h] = (fixed[h], "fixed")
        elif h in labels:
            out[h] = (seg.html, "speaker")
    return out
