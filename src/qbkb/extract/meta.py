"""元数据：JSON-LD → meta 标签 → 站点规则 → trafilatura，按字段取第一个有值的来源。"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterator
from copy import deepcopy
from datetime import date, datetime
from typing import Any

import trafilatura
from lxml.html import HtmlElement

from qbkb.extract.rules import ExtractRules, FieldRule, Template
from qbkb.models import ArticleMeta

# 这些 @type 的节点才当作"文章本身"；WebPage 之类只在找不到文章节点时用。
ARTICLE_TYPES = frozenset(
    {
        "Article",
        "NewsArticle",
        "BlogPosting",
        "Report",
        "ScholarlyArticle",
        "TechArticle",
        "AnalysisNewsArticle",
        "OpinionNewsArticle",
    }
)
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_WS = re.compile(r"\s+")


def _text(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _jtext(s: str) -> str:
    """JSON-LD 里的字符串：有的站点（Yoast 等）写成 HTML 实体（&#8220;、&amp;），json 不解码。"""
    return _text(html.unescape(s))


def _types(node: dict[str, Any]) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t}
    if isinstance(t, list):
        return {x for x in t if isinstance(x, str)}
    return set()


def _jsonld_nodes(doc: HtmlElement) -> Iterator[dict[str, Any]]:
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text or "", strict=False)
        except ValueError:
            continue  # 站点偶尔输出坏 JSON，跳过这一块，不影响其他来源
        stack: list[Any] = [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)


def _jsonld_article(doc: HtmlElement) -> tuple[dict[str, Any] | None, dict[str, dict[str, Any]]]:
    nodes = list(_jsonld_nodes(doc))
    by_id = {n["@id"]: n for n in nodes if isinstance(n.get("@id"), str)}
    for n in nodes:
        if _types(n) & ARTICLE_TYPES:
            return n, by_id
    for n in nodes:
        if "WebPage" in _types(n) and n.get("headline"):
            return n, by_id
    return None, by_id


def _names(value: Any, by_id: dict[str, dict[str, Any]]) -> list[str]:
    """JSON-LD 的 author 可能是字符串、对象、@id 引用或它们的列表。"""
    if isinstance(value, str):
        return [_jtext(value)] if value.strip() else []
    if isinstance(value, list):
        return [name for v in value for name in _names(v, by_id)]
    if isinstance(value, dict):
        name = value.get("name")
        if not name and isinstance(value.get("@id"), str):
            name = by_id.get(value["@id"], {}).get("name")
        return [_jtext(name)] if isinstance(name, str) and name.strip() else []
    return []


def _keywords(value: Any, *, split: bool = True, unescape: bool = False) -> list[str]:
    """字符串按逗号拆开（split=False 时整串算一个，比如栏目名里可能有逗号），列表逐项取。"""
    clean = _jtext if unescape else _text
    if isinstance(value, str):
        parts = value.split(",") if split else [value]
        return [clean(k) for k in parts if k.strip()]
    if isinstance(value, list):
        return [clean(k) for k in value if isinstance(k, str) and k.strip()]
    return []


_URL_LIKE = re.compile(r"^(?:https?://|www\.)", re.I)


def _real_authors(names: list[str], publishers: set[str]) -> list[str]:
    """去掉不是真人作者的值：和站点名 / 发布机构同名的（WordPress 账号名常常就是机构名）、网址。"""
    return [n for n in names if n.casefold() not in publishers and not _URL_LIKE.match(n)]


def _meta(doc: HtmlElement, *keys: str) -> list[str]:
    """按 property / name 取 meta 标签的 content，保持文档顺序。"""
    out: list[str] = []
    for m in doc.iter("meta"):
        key = m.get("property") or m.get("name")
        content = m.get("content")
        if key in keys and content and content.strip():
            out.append(_text(content))
    return out


_BR = "\ue000"  # 私用区字符当占位：不是空白，不会被 \s 折叠掉


def _element_text(el: HtmlElement) -> str:
    """元素的文字，空白折叠成一个空格；<br> 变成换行。

    "姓名<br/>职位" 直接 text_content() 会粘成"姓名职位"，规则的 pattern 就切不开了；
    换成换行后可以用 '^([^\\n]+)' 只取第一行。没有 <br> 时和 text_content() 折叠空白的结果一样。
    """
    if el.find(".//br") is None:
        return _text(el.text_content())
    dup = deepcopy(el)
    for br in dup.iter("br"):
        br.tail = _BR + (br.tail or "")
    lines = (_text(part) for part in dup.text_content().split(_BR))
    return "\n".join(line for line in lines if line)


def _rule_values(doc: HtmlElement, rule: FieldRule | None) -> list[str]:
    if rule is None:
        return []
    out: list[str] = []
    for el in doc.cssselect(rule.selector):
        value = _element_text(el)
        if rule.pattern is not None:
            m = rule.pattern.search(value)
            value = _text(m.group(1)) if m else ""
        else:
            value = _text(value)
        if value:
            out.append(value)
    return out


def normalize_date(value: str | None, formats: tuple[str, ...]) -> str | None:
    """统一成 YYYY-MM-DD。ISO 时间戳只取日期部分（站点本地日期，不换算时区）。"""
    if not value:
        return None
    value = _text(value)
    m = _ISO_DATE.match(value)
    if m:
        try:
            return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:
            return None  # 2023-00-00 这类无效日期：当作没有，调用方接着用下一个来源
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# trafilatura 有时把作者卡片的小标题一起当成作者（"About the author Jane Doe ..."）
_AUTHOR_HEADING = re.compile(r"^\s*(?:about\s+the\s+authors?|written\s+by|by)\b[\s:：]*", re.I)


def clean_author(name: str) -> str:
    return _text(_AUTHOR_HEADING.sub("", name))


def _dedupe(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))


def extract_meta(
    doc: HtmlElement, raw_html: str, url: str, rules: ExtractRules, template: Template | None
) -> ArticleMeta:
    node, by_id = _jsonld_article(doc)
    node = node or {}
    formats = rules.date_formats

    def strip_title(t: str) -> str:
        return _text(rules.title_strip.sub("", t)) if rules.title_strip else t

    titles: list[str] = []
    if isinstance(node.get("headline"), str):
        titles.append(_jtext(node["headline"]))
    titles += [strip_title(t) for t in _meta(doc, "og:title", "twitter:title")]
    titles += _rule_values(doc, template.title if template else None)

    dates: list[str | None] = []
    for key in ("datePublished", "dateCreated"):
        if isinstance(node.get(key), str):
            dates.append(normalize_date(node[key], formats))
    dates += [normalize_date(d, formats) for d in _meta(doc, "article:published_time")]
    dates += [
        normalize_date(d, formats)
        for d in _rule_values(doc, template.published if template else None)
    ]

    publishers = {
        n.casefold() for n in _meta(doc, "og:site_name") + _names(node.get("publisher"), by_id)
    }
    authors = _real_authors(_names(node.get("author"), by_id), publishers)
    if not authors:
        authors = _real_authors(_meta(doc, "author", "article:author"), publishers)
    if not authors:
        authors = _rule_values(doc, template.author if template else None)
    for rule in rules.author_fallback:
        if authors:
            break
        authors = _rule_values(doc, rule)

    # JSON-LD 的栏目（articleSection）排在关键词前面，一起当标签
    sections = _keywords(node.get("articleSection"), split=False, unescape=True)
    tags = _keywords(node.get("keywords"), unescape=True)
    if not tags:
        tags = _meta(doc, "article:tag")
    if not tags:
        tags = [k for v in _meta(doc, "keywords") for k in _keywords(v)]
    if not tags:
        tags = _rule_values(doc, template.tags if template else None)

    tags = sections + tags

    title = next((t for t in titles if t), None)
    published = next((d for d in dates if d), None)
    if title is None or published is None:
        # 最后才用 trafilatura 的启发式猜测，它对模板站点经常猜错
        fallback = trafilatura.extract_metadata(raw_html, default_url=url)
        if fallback is not None:
            if title is None and fallback.title:
                title = strip_title(_text(fallback.title))
            if published is None:
                published = normalize_date(fallback.date, formats)
            if not authors and fallback.author:
                authors = _real_authors(
                    [clean_author(a) for a in fallback.author.split(";")], publishers
                )

    title_doc = doc.find(".//title")
    if not title and title_doc is not None and title_doc.text:
        title = strip_title(_text(title_doc.text))

    return ArticleMeta(
        title=title or "",
        author=", ".join(_dedupe([clean_author(x) for x in authors])) or None,
        published=published,
        tags=_dedupe(tags),
    )
