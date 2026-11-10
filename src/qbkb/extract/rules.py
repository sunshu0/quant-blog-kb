"""解析站点配置里的 extract 节（config/sites/<site>.yaml）。

配置写错时尽早报错，并指出是哪一项，免得跑到一半才发现选择器没生效。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from cssselect import SelectorError
from lxml.cssselect import CSSSelector

from qbkb.config import SiteConfig

# 不管哪个站点都要删的东西：脚本、表单、嵌入播放器等，翻译不了也没有正文价值。
DEFAULT_REMOVE: tuple[str, ...] = (
    "script",
    "style",
    "template",
    "iframe",
    "object",
    "embed",
    "video",
    "audio",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "nav",
    "svg",
    "canvas",
    "link",
    "meta",
    "base",
)
# 版权声明要原样保留，只做安全清理：删掉能执行代码或加载外部资源的元素
KEEP_SAFETY_REMOVE: tuple[str, ...] = (
    "script",
    "style",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
    "link",
    "meta",
    "base",
    "form",
)
# 按整词、不分大小写匹配（"cookie jar" 不算 "cookies"，见 quality.py）
DEFAULT_NOISE_KEYWORDS: tuple[str, ...] = (
    "You are leaving",
    "accept cookies",
    "cookie policy",
    "cookie settings",
)
DEFAULT_MIN_CHARS = 500
# 页面上直接显示的日期（"3 December 2020" 这类）的解析格式
DEFAULT_DATE_FORMATS: tuple[str, ...] = ("%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y")


@dataclass(frozen=True, slots=True)
class FieldRule:
    """一个元数据字段的站点规则：取 selector 全部匹配的文字，可选正则取第一个分组。"""

    selector: str
    pattern: re.Pattern[str] | None = None


@dataclass(frozen=True, slots=True)
class Template:
    """一种页面模板的抽取规则。url_prefix 和 require 都满足才算匹配。"""

    name: str
    body: tuple[str, ...]
    url_prefix: str | None = None
    require: str | None = None
    title: FieldRule | None = None
    published: FieldRule | None = None
    author: FieldRule | None = None
    tags: FieldRule | None = None
    remove: tuple[str, ...] = ()

    def matches(self, url: str, has_selector: Callable[[str], bool]) -> bool:
        if self.url_prefix is not None and not urlsplit(url).path.startswith(self.url_prefix):
            return False
        return self.require is None or has_selector(self.require)


@dataclass(frozen=True, slots=True)
class KeepRule:
    """一块要原样附在正文后的内容（版权声明等）。

    selector 的全部匹配都会附上（嵌套在已选块里的、内容和前面完全相同的跳过）；
    remove 只删这块里明确列出的东西（比如响应式布局重复的标题），不套用全站噪音规则。
    """

    selector: str
    remove: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractRules:
    """整个站点的抽取规则。"""

    site: str
    templates: tuple[Template, ...] = ()
    remove: tuple[str, ...] = DEFAULT_REMOVE
    keep: tuple[KeepRule, ...] = ()
    noise_keywords: tuple[str, ...] = DEFAULT_NOISE_KEYWORDS
    min_chars: int = DEFAULT_MIN_CHARS
    title_strip: re.Pattern[str] | None = None
    drop_text: tuple[re.Pattern[str], ...] = ()
    date_formats: tuple[str, ...] = DEFAULT_DATE_FORMATS


def _err(path: str, msg: str) -> ValueError:
    return ValueError(f"extract.{path}: {msg}")


def _selector(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _err(path, "必须是非空的 CSS 选择器字符串")
    try:
        CSSSelector(value)
    except SelectorError as e:
        raise _err(path, f"CSS 选择器无效：{value!r}（{e}）") from e
    return value


def _selectors(value: Any, path: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise _err(path, "必须是选择器或选择器列表")
    return tuple(_selector(v, f"{path}[{i}]") for i, v in enumerate(value))


def _regex(value: Any, path: str) -> re.Pattern[str] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _err(path, "必须是正则表达式字符串")
    try:
        return re.compile(value)
    except re.error as e:
        raise _err(path, f"正则无效：{e}") from e


def _regexes(value: Any, path: str) -> tuple[re.Pattern[str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _err(path, "必须是正则表达式列表")
    out: list[re.Pattern[str]] = []
    for i, v in enumerate(value):
        pattern = _regex(v, f"{path}[{i}]")
        if pattern is None:
            raise _err(f"{path}[{i}]", "不能为空")
        out.append(pattern)
    return tuple(out)


def _field(value: Any, path: str) -> FieldRule | None:
    if value is None:
        return None
    if isinstance(value, str):
        return FieldRule(_selector(value, path))
    if isinstance(value, Mapping):
        pattern = _regex(value.get("pattern"), f"{path}.pattern")
        if pattern is not None and pattern.groups < 1:
            raise _err(f"{path}.pattern", "需要一个捕获组")
        return FieldRule(_selector(value.get("selector"), f"{path}.selector"), pattern)
    raise _err(path, "必须是选择器，或 {selector, pattern} 映射")


def _template(value: Any, i: int) -> Template:
    path = f"templates[{i}]"
    if not isinstance(value, Mapping):
        raise _err(path, "必须是映射")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise _err(f"{path}.name", "必须是非空字符串")
    body = _selectors(value.get("body"), f"{path}.body")
    if not body:
        raise _err(f"{path}.body", "至少要有一个正文容器选择器")
    prefix = value.get("url_prefix")
    if prefix is not None and not isinstance(prefix, str):
        raise _err(f"{path}.url_prefix", "必须是字符串")
    require = value.get("require")
    return Template(
        name=name,
        body=body,
        url_prefix=prefix,
        require=None if require is None else _selector(require, f"{path}.require"),
        title=_field(value.get("title"), f"{path}.title"),
        published=_field(value.get("published"), f"{path}.published"),
        author=_field(value.get("author"), f"{path}.author"),
        tags=_field(value.get("tags"), f"{path}.tags"),
        remove=_selectors(value.get("remove"), f"{path}.remove"),
    )


def _keep(value: Any) -> tuple[KeepRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise _err("keep", "必须是列表")
    out: list[KeepRule] = []
    for i, item in enumerate(value):
        path = f"keep[{i}]"
        if isinstance(item, Mapping):
            out.append(
                KeepRule(
                    _selector(item.get("selector"), f"{path}.selector"),
                    _selectors(item.get("remove"), f"{path}.remove"),
                )
            )
        else:
            out.append(KeepRule(_selector(item, path)))
    return tuple(out)


def load_rules(site: SiteConfig) -> ExtractRules:
    """从站点配置读出抽取规则。extract 节为空时只用 trafilatura 兜底。"""
    raw = site.raw.get("extract")
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise _err("", "extract 节必须是映射")
    templates = raw.get("templates", [])
    if not isinstance(templates, list):
        raise _err("templates", "必须是列表")
    keywords = raw.get("noise_keywords", list(DEFAULT_NOISE_KEYWORDS))
    if not isinstance(keywords, list) or not all(isinstance(k, str) and k for k in keywords):
        raise _err("noise_keywords", "必须是非空字符串列表")
    min_chars = raw.get("min_chars", DEFAULT_MIN_CHARS)
    if not isinstance(min_chars, int) or isinstance(min_chars, bool) or min_chars < 0:
        raise _err("min_chars", "必须是非负整数")
    formats = raw.get("date_formats")
    if formats is not None and (
        not isinstance(formats, list) or not all(isinstance(f, str) for f in formats)
    ):
        raise _err("date_formats", "必须是 strptime 格式字符串列表")
    return ExtractRules(
        site=site.name,
        templates=tuple(_template(t, i) for i, t in enumerate(templates)),
        remove=DEFAULT_REMOVE + _selectors(raw.get("remove"), "remove"),
        keep=_keep(raw.get("keep")),
        noise_keywords=tuple(keywords),
        min_chars=min_chars,
        title_strip=_regex(raw.get("title_strip"), "title_strip"),
        drop_text=_regexes(raw.get("drop_text"), "drop_text"),
        date_formats=DEFAULT_DATE_FORMATS if formats is None else tuple(formats),
    )
