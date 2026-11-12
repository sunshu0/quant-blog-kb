"""解析站点配置里的 sections 节（design.md 4.9）：栏目定义、识别规则、是否采集、调研记录。

栏目怎么识别，按顺序：
1. 网址前缀（url_prefix）：发现阶段就能定，不用抓页面；
2. 页面元数据（page_key）：抓取并抽取后，从页面上读一段文字，按正则取出若干部分拼成栏目标识
   （例如 Man 的 tagline "ARTICLE | 8 MIN | VIEWS FROM THE FLOOR"
   → "ARTICLE | VIEWS FROM THE FLOOR"），
   再和配置里每个栏目的 key 比较。

站点配置没有 sections 节时不分栏目：全部文章都采集（老行为）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from cssselect import SelectorError
from lxml.cssselect import CSSSelector
from lxml.html import HtmlElement

from qbkb.config import SiteConfig

URL_KEY_PREFIX = "url:"
_WS = re.compile(r"\s+")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
KEY_SEP = " | "


def _err(path: str, msg: str) -> ValueError:
    return ValueError(f"sections.{path}: {msg}")


def normalize_key(text: str) -> str:
    """栏目标识统一成：各部分去首尾空白、折叠空白、大写，用 " | " 连接。"""
    parts = [_WS.sub(" ", p).strip().upper() for p in text.split("|")]
    return KEY_SEP.join(p for p in parts if p)


def prefix_matches(path: str, prefix: str) -> bool:
    """/insights/x 匹配它自己和它下面的路径（/insights/x/y），不匹配 /insights/x-y。"""
    p = prefix.rstrip("/")
    return path == p or path.startswith(p + "/")


def last_segment(url: str) -> str:
    """网址路径的最后一段（估计栏目规模时按它匹配，例如 views-from-the-floor-2025-march-18）。"""
    parts = [s for s in urlsplit(url).path.split("/") if s]
    return parts[-1].lower() if parts else ""


@dataclass(frozen=True, slots=True)
class Section:
    """一个栏目。

    url_prefix / key 二选一：url_prefix 按网址判定，key 按页面元数据判定。
    estimate：没抓到页面时，按网址最后一段匹配这些正则来**估计**篇数（只用于栏目调研，
    不用于采集判定）。
    """

    id: str
    name: str
    collect: bool
    url_prefix: str | None = None
    key: str | None = None
    status: str | None = None  # 例如 "待调研" / "已调研"
    survey: Mapping[str, Any] = field(default_factory=dict[str, Any])
    estimate: tuple[re.Pattern[str], ...] = ()

    def matches_url(self, url: str) -> bool:
        return self.url_prefix is not None and prefix_matches(urlsplit(url).path, self.url_prefix)

    @property
    def url_key(self) -> str | None:
        return None if self.url_prefix is None else URL_KEY_PREFIX + self.url_prefix.rstrip("/")


@dataclass(frozen=True, slots=True)
class PageKeyRule:
    """从页面上读栏目标识：selector 第一个匹配的文字；pattern 的命名分组按 parts 的顺序拼起来。"""

    selector: str
    pattern: re.Pattern[str] | None = None
    parts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CrossCheck:
    """核对：原始 HTML 里按 pattern 取一个值（第一个分组），和栏目标识里 part 那一部分比较。

    不一致时记一条质量提示，栏目仍以 page_key 为准。
    """

    pattern: re.Pattern[str]
    part: str
    label: str
    # 算作一致的组合（大写）：page_key 的值 → 另一来源可以是这些值
    equivalent: Mapping[str, frozenset[str]] = field(default_factory=dict[str, frozenset[str]])

    def agrees(self, mine: str, theirs: str) -> bool:
        m, t = mine.upper(), theirs.upper()
        return m == t or t in self.equivalent.get(m, frozenset())


@dataclass(frozen=True, slots=True)
class SectionRules:
    site: str
    enabled: bool = False
    sections: tuple[Section, ...] = ()
    page_key: PageKeyRule | None = None
    cross_check: CrossCheck | None = None

    def by_id(self, section_id: str | None) -> Section | None:
        return next((s for s in self.sections if s.id == section_id), None)

    def by_url(self, url: str) -> Section | None:
        return next((s for s in self.sections if s.matches_url(url)), None)

    def by_key(self, key: str | None) -> Section | None:
        if not key or key.startswith(URL_KEY_PREFIX):
            return None
        k = normalize_key(key)
        return next((s for s in self.sections if s.key == k), None)

    def classify(self, url: str, key: str | None) -> Section | None:
        """网址前缀优先，其次页面上读到的栏目标识。"""
        return self.by_url(url) or self.by_key(key)

    def collects(self, url: str, key: str | None) -> bool:
        """这篇文章要不要往下走（翻译、导出）。不分栏目的站点全部要。"""
        if not self.enabled:
            return True
        s = self.classify(url, key)
        return s is not None and s.collect

    def estimate(self, url: str) -> Section | None:
        """没抓页面时按网址估计属于哪个栏目（前缀栏目是确定的，其余按 estimate 正则）。"""
        s = self.by_url(url)
        if s is not None:
            return s
        seg = last_segment(url)
        return next((s for s in self.sections if any(p.search(seg) for p in s.estimate)), None)

    def name_of(self, section_id: str | None) -> str | None:
        s = self.by_id(section_id)
        return None if s is None else s.name


def _selector(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _err(path, "必须是非空的 CSS 选择器字符串")
    try:
        CSSSelector(value)
    except SelectorError as e:
        raise _err(path, f"CSS 选择器无效：{value!r}（{e}）") from e
    return value


def _regex(value: Any, path: str) -> re.Pattern[str]:
    if not isinstance(value, str) or not value:
        raise _err(path, "必须是非空的正则表达式字符串")
    try:
        return re.compile(value, re.IGNORECASE)
    except re.error as e:
        raise _err(path, f"正则无效：{e}") from e


def _page_key(value: Any) -> PageKeyRule | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _err("page_key", "必须是 {selector, pattern, parts} 映射")
    selector = _selector(value.get("selector"), "page_key.selector")
    raw_pattern = value.get("pattern")
    if raw_pattern is None:
        return PageKeyRule(selector)
    pattern = _regex(raw_pattern, "page_key.pattern")
    parts = value.get("parts")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, str) for p in parts):
        raise _err("page_key.parts", "有 pattern 时必须列出要拼成栏目标识的分组名")
    missing = [p for p in parts if p not in pattern.groupindex]
    if missing:
        raise _err("page_key.parts", f"正则里没有这些命名分组：{missing}")
    return PageKeyRule(selector, pattern, tuple(parts))


def _cross_check(value: Any, page_key: PageKeyRule | None) -> CrossCheck | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise _err("cross_check", "必须是 {pattern, part, label} 映射")
    pattern = _regex(value.get("pattern"), "cross_check.pattern")
    if pattern.groups < 1:
        raise _err("cross_check.pattern", "需要一个捕获组")
    part = value.get("part")
    if page_key is None or part not in page_key.parts:
        raise _err("cross_check.part", "必须是 page_key.parts 里的一个分组名")
    label = value.get("label") or "页面元数据"
    raw_eq = value.get("equivalent") or {}
    if not isinstance(raw_eq, Mapping):
        raise _err("cross_check.equivalent", "必须是 值 → [也算一致的值] 的映射")
    equivalent: dict[str, frozenset[str]] = {}
    for k, v in raw_eq.items():
        vals = [v] if isinstance(v, str) else v
        if not isinstance(vals, list) or not all(isinstance(x, str) for x in vals):
            raise _err(f"cross_check.equivalent.{k}", "必须是字符串或字符串列表")
        equivalent[str(k).upper()] = frozenset(x.upper() for x in vals)
    return CrossCheck(pattern, str(part), str(label), equivalent)


def _section(value: Any, i: int, seen: set[str]) -> Section:
    path = f"list[{i}]"
    if not isinstance(value, Mapping):
        raise _err(path, "必须是映射")
    sid = value.get("id")
    if not isinstance(sid, str) or not _ID.match(sid):
        raise _err(f"{path}.id", "必须是小写字母、数字、- 和 _ 组成的字符串")
    if sid in seen:
        raise _err(f"{path}.id", f"重复的栏目 id：{sid}")
    seen.add(sid)
    name = value.get("name") or sid
    collect = value.get("collect")
    if not isinstance(collect, bool):
        raise _err(f"{path}.collect", "必须写明 true 或 false")
    prefix = value.get("url_prefix")
    key = value.get("key")
    if (prefix is None) == (key is None):
        raise _err(path, "url_prefix 和 key 必须写且只写一个")
    if prefix is not None and (not isinstance(prefix, str) or not prefix.startswith("/")):
        raise _err(f"{path}.url_prefix", "必须以 / 开头")
    if key is not None and (not isinstance(key, str) or not normalize_key(key)):
        raise _err(f"{path}.key", "必须是非空字符串")
    estimate = value.get("estimate") or []
    if not isinstance(estimate, list):
        raise _err(f"{path}.estimate", "必须是正则列表")
    survey = value.get("survey") or {}
    if not isinstance(survey, Mapping):
        raise _err(f"{path}.survey", "必须是映射")
    status = value.get("status")
    return Section(
        id=sid,
        name=str(name),
        collect=collect,
        url_prefix=prefix,
        key=None if key is None else normalize_key(key),
        status=None if status is None else str(status),
        survey=dict(survey),
        estimate=tuple(_regex(p, f"{path}.estimate[{j}]") for j, p in enumerate(estimate)),
    )


def load_section_rules(site: SiteConfig) -> SectionRules:
    raw = site.raw.get("sections")
    if raw is None:
        return SectionRules(site=site.name)
    if not isinstance(raw, Mapping):
        raise _err("", "sections 节必须是映射")
    items = raw.get("list")
    if not isinstance(items, list) or not items:
        raise _err("list", "至少要有一个栏目")
    seen: set[str] = set()
    sections = tuple(_section(v, i, seen) for i, v in enumerate(items))
    keys = [s.key for s in sections if s.key is not None]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise _err("list", f"栏目标识重复：{dup}")
    page_key = _page_key(raw.get("page_key"))
    if keys and page_key is None:
        raise _err("page_key", "有按 key 识别的栏目时必须配置 page_key")
    return SectionRules(
        site=site.name,
        enabled=True,
        sections=sections,
        page_key=page_key,
        cross_check=_cross_check(raw.get("cross_check"), page_key),
    )


@dataclass(frozen=True, slots=True)
class PageSection:
    """一个页面的栏目判定结果。key 是 "" 表示读不到；issues 写进 qc_issues。"""

    key: str
    issues: tuple[str, ...] = ()


def page_section(doc: HtmlElement, raw_html: str, url: str, rules: SectionRules) -> PageSection:
    """读页面判定栏目标识（确定性规则，不调用 LLM）。不分栏目的站点返回空标识。"""
    if not rules.enabled:
        return PageSection("")
    by_url = rules.by_url(url)
    if by_url is not None and by_url.url_key is not None:
        return PageSection(by_url.url_key)
    rule = rules.page_key
    if rule is None:
        return PageSection("", ("没有栏目识别规则（sections.page_key），按未知栏目处理",))
    found = doc.cssselect(rule.selector)
    text = _WS.sub(" ", found[0].text_content()).strip() if found else ""
    if not text:
        return PageSection("", (f"页面上没有读到栏目信息（{rule.selector}）",))
    if rule.pattern is None:
        return PageSection(normalize_key(text))
    m = rule.pattern.search(text)
    if m is None:
        return PageSection(normalize_key(text), (f"栏目信息的格式不认识：{text!r}",))
    values = {p: (m.group(p) or "").strip() for p in rule.parts}
    key = normalize_key(KEY_SEP.join(v for v in values.values() if v))
    issues: list[str] = []
    check = rules.cross_check
    if check is not None:
        cm = check.pattern.search(raw_html)
        mine = values.get(check.part, "")
        theirs = (cm.group(1) or "").strip() if cm else ""
        if theirs and mine and not check.agrees(mine, theirs):
            issues.append(
                f"栏目核对不一致：{check.label} 是 {theirs}，{rule.selector} 是 {mine.upper()}，"
                f"以后者为准"
            )
    return PageSection(key, tuple(issues))
