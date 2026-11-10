"""术语表：读取 config/glossary.yaml（design.md 4.5）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml

from qbkb.config import CONFIG_DIR
from qbkb.translate.structure import plain_text

# 段落术语版本的规则版本。匹配规则或大小写变体规则改了就递增，让所有含术语的段落重翻。
SEGMENT_RULES = "g1"
NO_TERMS = f"{SEGMENT_RULES}-none"


def terms_fingerprint(terms: Mapping[str, str]) -> str:
    """术语内容的指纹（与顺序无关）。"""
    payload = json.dumps(sorted(terms.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def case_variants(terms: Mapping[str, str]) -> dict[str, str]:
    """DeepL 术语表区分大小写：为每个词条补上句首大写和标题大写的变体。

    "drawdown" → 另加 "Drawdown"；"trend following" → 另加 "Trend following"、"Trend Following"。
    原文和译文相同的词条（alpha → alpha），译文跟着原文变；yaml 里显式写了的变体优先。
    DeepL 后端和 FakeTranslator 都用这份展开结果，两边的匹配行为一致。
    """
    out = dict(terms)
    for src, tgt in terms.items():
        for variant in (
            src[:1].upper() + src[1:],
            " ".join(w[:1].upper() + w[1:] for w in src.split()),
        ):
            if variant not in out:
                out[variant] = variant if tgt == src else tgt
    return out


class Glossary(Mapping[str, str]):
    """英文 → 中文术语表。本身就是 Mapping，可以直接传给 Translator.translate。

    翻译记忆的 glossary_version **按段落计算**（version_for）：只由这段里实际出现的术语决定。
    所以术语表改了，只有含相关术语的段落会重翻，不会让整个翻译记忆失效。
    yaml 里的 version 只用来给 DeepL 上的术语表起名，方便人看。
    """

    def __init__(self, version: str, terms: Mapping[str, str]) -> None:
        self.version = version
        self._terms = dict(terms)
        self.fingerprint = terms_fingerprint(self._terms)
        # 大小写不敏感、只要求左边界（"factor" 也匹配 "factors"）：宁可多算，不可漏算
        self._patterns = [
            (src, re.compile(rf"(?<!\w){re.escape(src)}", re.IGNORECASE)) for src in self._terms
        ]

    def terms_in(self, html: str) -> dict[str, str]:
        """这段 HTML 正文里出现的术语。"""
        text = plain_text(html)
        return {src: self._terms[src] for src, pat in self._patterns if pat.search(text)}

    def version_for(self, html: str) -> str:
        """这一段的术语版本，写进翻译记忆的 glossary_version 列。"""
        used = self.terms_in(html)
        return f"{SEGMENT_RULES}-{terms_fingerprint(used)}" if used else NO_TERMS

    def __getitem__(self, key: str) -> str:
        return self._terms[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._terms)

    def __len__(self) -> int:
        return len(self._terms)

    def __repr__(self) -> str:
        return f"Glossary(version={self.version!r}, terms={len(self)})"


def load_glossary(path: Path | None = None) -> Glossary:
    path = path or CONFIG_DIR / "glossary.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "version" not in data:
        raise ValueError(f"{path}: 顶层必须是映射，并且有 version")
    raw_terms = data.get("terms") or {}
    if not isinstance(raw_terms, dict):
        raise ValueError(f"{path}: terms 必须是映射")
    terms: dict[str, str] = {}
    for src, tgt in raw_terms.items():
        # yaml 会把 yes/no/数字之类解析成别的类型，这里统一要求写成字符串
        if not isinstance(src, str) or not isinstance(tgt, str):
            raise ValueError(f"{path}: 术语 {src!r} 的原文和译文都必须是字符串")
        if not src.strip() or not tgt.strip() or src != src.strip() or tgt != tgt.strip():
            # DeepL 术语表不接受空词条和首尾空白
            raise ValueError(f"{path}: 术语 {src!r} 为空或带首尾空白")
        terms[src] = tgt
    return Glossary(str(data["version"]), terms)
