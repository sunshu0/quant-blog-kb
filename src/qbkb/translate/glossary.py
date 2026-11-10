"""术语表：读取 config/glossary.yaml（design.md 4.5）。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml

from qbkb.config import CONFIG_DIR


def terms_fingerprint(terms: Mapping[str, str]) -> str:
    """术语内容的指纹（与顺序无关）。"""
    payload = json.dumps(sorted(terms.items()), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


class Glossary(Mapping[str, str]):
    """英文 → 中文术语表。本身就是 Mapping，可以直接传给 Translator.translate。

    tm_version 写进翻译记忆的 glossary_version 列：由 yaml 里的 version 加内容指纹组成，
    所以改了术语却忘了递增 version，也不会误用旧译文。
    """

    def __init__(self, version: str, terms: Mapping[str, str]) -> None:
        self.version = version
        self._terms = dict(terms)
        self.fingerprint = terms_fingerprint(self._terms)

    @property
    def tm_version(self) -> str:
        return f"{self.version}-{self.fingerprint}"

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
