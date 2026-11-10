"""抽取入口：原始 HTML → ExtractedArticle（design.md 4.3）。

站点规则优先；没有模板匹配、或模板的正文容器一个都没找到时，用 trafilatura 兜底。
版权声明（规则里的 keep）不管走哪条路都原样附在正文后面。
"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura
from lxml.html import HtmlElement, document_fromstring

from qbkb.extract.clean import (
    block_text,
    finish,
    prepare,
    referenced_ids,
    split_blocks,
    to_segments,
)
from qbkb.extract.meta import extract_meta
from qbkb.extract.rules import ExtractRules, Template
from qbkb.models import ExtractedArticle

FALLBACK = "trafilatura"


class ExtractError(Exception):
    """这篇文章抽不出正文。"""


@dataclass(frozen=True, slots=True)
class Extraction:
    """抽取结果加上质检要用的信息。

    n_body：前 n_body 个段落是正文，后面的是版权声明等附录（keep）。
    method：用了哪条规则，"rule:<模板名>" 或 "trafilatura"。
    """

    article: ExtractedArticle
    method: str
    n_body: int
    keep_found: bool


def _first(doc: HtmlElement, selector: str) -> HtmlElement | None:
    found = doc.cssselect(selector)
    return found[0] if found else None


def _match_template(doc: HtmlElement, url: str, rules: ExtractRules) -> Template | None:
    for t in rules.templates:
        if t.matches(url, lambda sel: _first(doc, sel) is not None):
            return t
    return None


def _fallback_root(raw: str, url: str) -> HtmlElement | None:
    out = trafilatura.extract(
        raw,
        url=url,
        output_format="html",
        include_comments=False,
        include_tables=True,
        include_images=True,
        include_links=True,
        include_formatting=True,
    )
    if not out:
        return None
    body = document_fromstring(out).find("body")
    return body


def _drop_by_text(blocks: list[str], rules: ExtractRules) -> list[str]:
    """去掉纯文字匹配 drop_text 的段落（选择器区分不出来的模板文字）。"""
    if not rules.drop_text:
        return blocks
    return [b for b in blocks if not any(p.search(block_text(b)) for p in rules.drop_text)]


def parse(raw: str | bytes) -> HtmlElement:
    """bytes 交给 lxml 按 <meta charset> 解码；str 原样解析。"""
    return document_fromstring(raw)


def extract_full(raw: str | bytes, url: str, rules: ExtractRules) -> Extraction:
    doc = parse(raw)
    raw_text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
    template = _match_template(doc, url, rules)
    meta = extract_meta(doc, raw_text, url, rules, template)

    body_roots: list[HtmlElement] = []
    method = FALLBACK
    if template is not None:
        remove = rules.remove + template.remove
        for sel in template.body:
            el = _first(doc, sel)
            if el is not None:
                body_roots.append(prepare(el, url, remove))
        if body_roots:
            method = f"rule:{template.name}"
    if not body_roots:
        fb = _fallback_root(raw_text, url)
        if fb is not None:
            root = prepare(fb, url, rules.remove)
            # trafilatura 会把页面大标题也放进正文；标题已经在元数据里，不再重复成一段
            for h1 in root.findall("h1"):
                if " ".join(h1.text_content().split()) == meta.title:
                    h1.drop_tree()
            body_roots.append(root)

    keep_roots: list[HtmlElement] = []
    for sel in rules.keep:
        el = _first(doc, sel)
        if el is not None:
            keep_roots.append(prepare(el, url, rules.remove))

    keep_ids = referenced_ids(body_roots + keep_roots)
    body_blocks: list[str] = []
    for root in body_roots:
        body_blocks += split_blocks(finish(root, keep_ids))
    body_blocks = _drop_by_text(body_blocks, rules)
    if not body_blocks:
        raise ExtractError("没有抽到正文（站点规则和 trafilatura 都没有结果）")
    keep_blocks: list[str] = []
    for root in keep_roots:
        keep_blocks += split_blocks(finish(root, keep_ids))
    keep_blocks = _drop_by_text(keep_blocks, rules)

    article = ExtractedArticle(url=url, meta=meta, segments=to_segments(body_blocks + keep_blocks))
    return Extraction(
        article=article, method=method, n_body=len(body_blocks), keep_found=bool(keep_blocks)
    )


def extract(raw: str | bytes, url: str, rules: ExtractRules) -> ExtractedArticle:
    """原始 HTML + URL + 站点规则 → ExtractedArticle。抽不到正文时抛 ExtractError。"""
    return extract_full(raw, url, rules).article
