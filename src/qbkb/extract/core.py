"""抽取入口：原始 HTML → ExtractedArticle（design.md 4.3）。

站点规则优先；没有模板匹配、或模板的正文容器一个都没找到时，用 trafilatura 兜底。
版权声明（规则里的 keep）不管走哪条路都原样附在正文后面。
"""

from __future__ import annotations

import codecs
import contextlib
import re
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
from qbkb.extract.rules import KEEP_SAFETY_REMOVE, ExtractRules, Template
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


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_:.-]+)""", re.I)
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")
_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_html(raw: str | bytes) -> str:
    """按 BOM → 页面开头的 <meta charset> → UTF-8 的顺序解码。

    只解码一次，lxml、trafilatura 和元数据兜底拿到的是同一份文字，不会一边对一边乱码。
    """
    if isinstance(raw, str):
        text = raw
    else:
        encoding = "utf-8"
        for bom, name in _BOMS:
            if raw.startswith(bom):
                encoding = name
                break
        else:
            m = _META_CHARSET.search(raw[:4096])
            if m:
                # 写错的 charset 名字：按 UTF-8 处理
                with contextlib.suppress(LookupError):
                    encoding = codecs.lookup(m.group(1).decode("ascii")).name
        text = raw.decode(encoding, errors="replace")
    # lxml 不接受带编码声明的 str（<?xml encoding=...?>），去掉它
    return _XML_DECL.sub("", text, count=1)


def parse(raw: str | bytes) -> HtmlElement:
    return document_fromstring(decode_html(raw))


def _keep_roots(doc: HtmlElement, url: str, rules: ExtractRules) -> list[HtmlElement]:
    """版权声明块：每条规则的全部匹配，跳过嵌套在已选块里的和内容完全重复的。"""
    chosen: list[HtmlElement] = []
    roots: list[HtmlElement] = []
    seen: set[str] = set()
    for rule in rules.keep:
        for el in doc.cssselect(rule.selector):
            if any(el is c or c in el.iterancestors() for c in chosen):
                continue
            root = prepare(el, url, KEEP_SAFETY_REMOVE + rule.remove)
            key = " ".join(root.text_content().split())
            if not key or key in seen:
                continue  # 响应式布局里同一块内容出现多次，只要一份
            seen.add(key)
            chosen.append(el)
            roots.append(root)
    return roots


def extract_full(raw: str | bytes, url: str, rules: ExtractRules) -> Extraction:
    raw_text = decode_html(raw)
    doc = document_fromstring(raw_text)
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

    keep_roots = _keep_roots(doc, url, rules)
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

    article = ExtractedArticle(url=url, meta=meta, segments=to_segments(body_blocks + keep_blocks))
    return Extraction(
        article=article, method=method, n_body=len(body_blocks), keep_found=bool(keep_blocks)
    )


def extract(raw: str | bytes, url: str, rules: ExtractRules) -> ExtractedArticle:
    """原始 HTML + URL + 站点规则 → ExtractedArticle。抽不到正文时抛 ExtractError。"""
    return extract_full(raw, url, rules).article


def body_chars(raw: str | bytes, url: str, rules: ExtractRules) -> int:
    """正文段落（不含 keep 附录）的纯文字字符数。

    fetch 用它判断"网页正文是否够长，不需要再下载 PDF"，和 extract_one 里
    "正文太短就用 PDF"用的是同一套抽取逻辑、同一段口径（前 n_body 段）。
    """
    result = extract_full(raw, url, rules)
    return sum(len(block_text(s.html)) for s in result.article.segments[: result.n_body])
