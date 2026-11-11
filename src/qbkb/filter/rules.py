"""读取 filters.yaml、评分标准、人工覆盖和评测集（design.md 4.4）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from qbkb.config import CONFIG_DIR

DEFAULT_EXCERPT_WORDS = 500
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


@dataclass(frozen=True, slots=True)
class SiteRules:
    """L1 规则。"""

    exclude_prefixes: tuple[str, ...] = ()
    min_words: int = 0


@dataclass(frozen=True, slots=True)
class FilterConfig:
    include_categories: tuple[str, ...]
    exclude_categories: tuple[str, ...]
    rubric_version: str
    threshold: float
    sites: dict[str, SiteRules] = field(default_factory=dict[str, SiteRules])
    excerpt_words: int = DEFAULT_EXCERPT_WORDS
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL

    @property
    def categories(self) -> tuple[str, ...]:
        """L2 允许输出的全部类别。"""
        return self.include_categories + self.exclude_categories

    def site(self, name: str) -> SiteRules:
        return self.sites.get(name, SiteRules())

    def keeps(self, value: float, category: str) -> bool:
        """L2 判定：分数够、类别在收录列表里。"""
        return value >= self.threshold and category in self.include_categories


def _str_list(data: dict[str, Any], key: str, path: Path) -> tuple[str, ...]:
    items = data.get(key) or []
    if not isinstance(items, list) or not all(isinstance(x, str) and x for x in items):
        raise ValueError(f"{path}: {key} 必须是非空字符串的列表")
    return tuple(items)


def load_filter_config(path: Path | None = None) -> FilterConfig:
    path = path or CONFIG_DIR / "filters.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: 顶层必须是映射")
    include = _str_list(data, "include_categories", path)
    exclude = _str_list(data, "exclude_categories", path)
    if not include:
        raise ValueError(f"{path}: include_categories 不能为空")
    if set(include) & set(exclude):
        raise ValueError(f"{path}: 类别不能既收又不收：{sorted(set(include) & set(exclude))}")
    sites: dict[str, SiteRules] = {}
    for name, raw in (data.get("sites") or {}).items():
        raw = raw or {}
        prefixes = _str_list(raw, "exclude_prefixes", path)
        for p in prefixes:
            if not p.startswith("/"):
                raise ValueError(f"{path}: 站点 {name} 的路径前缀必须以 / 开头: {p!r}")
        sites[str(name)] = SiteRules(prefixes, int(raw.get("min_words", 0)))
    l2: dict[str, Any] = data.get("l2") or {}
    if "rubric_version" not in l2 or "threshold" not in l2:
        raise ValueError(f"{path}: l2 必须有 rubric_version 和 threshold")
    return FilterConfig(
        include_categories=include,
        exclude_categories=exclude,
        rubric_version=str(l2["rubric_version"]),
        threshold=float(l2["threshold"]),
        sites=sites,
        excerpt_words=int(l2.get("excerpt_words", DEFAULT_EXCERPT_WORDS)),
        base_url=str(l2.get("base_url") or DEFAULT_BASE_URL),
        model=str(l2.get("model") or DEFAULT_MODEL),
    )


@dataclass(frozen=True, slots=True)
class Rubric:
    """评分标准：版本号 + 提示词（已填入类别列表）。"""

    version: str
    prompt: str


def load_rubric(version: str, cfg: FilterConfig, rubric_dir: Path | None = None) -> Rubric:
    """读 config/rubrics/<version>.md。文件头的 version 必须和文件名一致，防止复制后忘了改。"""
    path = (rubric_dir or CONFIG_DIR / "rubrics") / f"{version}.md"
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise ValueError(f"{path}: 缺少 --- 开头的文件头")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError(f"{path}: 文件头没有结束的 ---")
    meta = yaml.safe_load(head) or {}
    if not isinstance(meta, dict) or str(meta.get("version")) != version:
        raise ValueError(f"{path}: 文件头的 version 应该是 {version!r}")
    prompt = (
        body.replace("{include_categories}", "、".join(cfg.include_categories))
        .replace("{exclude_categories}", "、".join(cfg.exclude_categories))
        .strip()
    )
    return Rubric(version=version, prompt=prompt)


class Decision(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"


@dataclass(frozen=True, slots=True)
class Override:
    """L3 人工覆盖的一条。"""

    decision: Decision
    category: str | None = None
    note: str | None = None


def _decision(value: object, where: str) -> Decision:
    try:
        return Decision(str(value))
    except ValueError:
        raise ValueError(f"{where}: 判断必须是 include 或 exclude，不是 {value!r}") from None


def load_overrides(path: Path | None = None) -> dict[str, Override]:
    """读 config/overrides.yaml。文件不存在就是没有覆盖。"""
    path = path or CONFIG_DIR / "overrides.yaml"
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = data.get("overrides") or {} if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: overrides 必须是 URL → 判断 的映射")
    out: dict[str, Override] = {}
    for url, value in raw.items():
        where = f"{path}: {url}"
        if isinstance(value, dict):
            category = value.get("category")
            note = value.get("note")
            out[str(url)] = Override(
                _decision(value.get("decision"), where),
                str(category) if category else None,
                str(note) if note else None,
            )
        else:
            out[str(url)] = Override(_decision(value, where))
    return out


def load_eval_set(path: Path) -> dict[str, Decision]:
    """评测集：URL → 人工判断。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, dict):
        raise ValueError(f"{path}: 需要 items: {{URL: include | exclude}}")
    return {str(url): _decision(v, f"{path}: {url}") for url, v in items.items()}
