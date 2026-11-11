"""术语表：读取 config/glossary.yaml（design.md 4.5）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

import yaml

from qbkb.config import CONFIG_DIR
from qbkb.translate.fixed import FIXED_FILE, FixedTranslations, load_fixed
from qbkb.translate.structure import plain_text

# 段落术语版本的规则版本。匹配规则或大小写变体规则改了就递增，让所有含术语的段落重翻。
# g2（2026-11-12）：最长匹配、缩写区分大小写。
SEGMENT_RULES = "g2"
# 以前用过的规则版本（估算升级成本时用来认出旧译文）
PREVIOUS_SEGMENT_RULES: tuple[str, ...] = ("g1",)
# 不含术语的段落：和匹配规则无关（规则变了它们也不用重翻），所以固定写成 g1 时期的值，
# 不跟着 SEGMENT_RULES 变，已有翻译记忆里的 "g1-none" 照样命中
NO_TERMS = "g1-none"


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


_ACRONYM = re.compile(r"^[A-Z][A-Z0-9/&]{1,5}$")


def _term_pattern(src: str) -> re.Pattern[str]:
    """terms_in 用的匹配规则。

    普通词条：大小写不敏感、只要求左边界（"value factor" 也匹配 "value factors"）——宁可多算。
    缩写（全大写、不超过 6 个字符，如 CLO、ROE、EV/S）：区分大小写、要求右边界（允许复数 s），
    否则 "CLO" 会匹配 "close"、"BDC" 之类的误算让大量段落白白重翻。DeepL 的术语表本来就区分大小写。
    """
    if _ACRONYM.match(src):
        return re.compile(rf"(?<!\w){re.escape(src)}s?(?!\w)")
    return re.compile(rf"(?<!\w){re.escape(src)}", re.IGNORECASE)


class Glossary(Mapping[str, str]):
    """英文 → 中文术语表。本身就是 Mapping，可以直接传给 Translator.translate。

    翻译记忆的 glossary_version **按段落计算**（version_for）：只由这段里实际出现的术语决定。
    所以术语表改了，只有含相关术语的段落会重翻，不会让整个翻译记忆失效。
    yaml 里的 version 只用来给 DeepL 上的术语表起名，方便人看。
    """

    def __init__(
        self,
        version: str,
        terms: Mapping[str, str],
        fixed: Mapping[str, str] | None = None,
    ) -> None:
        self.version = version
        self._terms = dict(terms)
        self.fingerprint = terms_fingerprint(self._terms)
        # 固定译文（fixed.py）：原文段落 hash → 中文 HTML。和术语表一样是"翻译资源"，
        # 放在这里是为了 translate / export / 额度估算都能拿到，不用多传一个参数
        self.fixed: Mapping[str, str] = FixedTranslations(fixed)
        # 大小写不敏感、只要求左边界（"value factor" 也匹配 "value factors"）：宁可多算，不可漏算
        # 长的先匹配：一处文字只算给最长的术语（"implied volatility" 里的 volatility 不单独算）
        self._patterns = [
            (src, _term_pattern(src)) for src in sorted(self._terms, key=len, reverse=True)
        ]

    def terms_in(self, html: str) -> dict[str, str]:
        """这段 HTML 正文里出现的术语。被更长的术语完全覆盖的出现不算（最长匹配）。"""
        text = plain_text(html)
        taken: list[tuple[int, int]] = []
        found: dict[str, str] = {}
        for src, pat in self._patterns:
            for m in pat.finditer(text):
                s, e = m.span()
                if any(a <= s and e <= b for a, b in taken):
                    continue
                taken.append((s, e))
                found[src] = self._terms[src]
        return found

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


def load_glossary(path: Path | None = None, fixed_path: Path | None = None) -> Glossary:
    """读术语表；同目录下的 fixed_translations.yaml（有的话）作为固定译文一起读进来。"""
    path = path or CONFIG_DIR / "glossary.yaml"
    fixed = load_fixed(fixed_path or path.parent / FIXED_FILE)
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
    return Glossary(str(data["version"]), terms, fixed)
