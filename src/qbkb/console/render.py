"""把 export 的产物（output/ 下的 Markdown）渲染成可以放进页面的 HTML。

为什么读 Markdown 而不是状态库：export 产物就是"知识库"本身
（术语注释、本地图片、版权说明都在里面），
控制台显示的和用户拿去用的是同一份东西。

安全：
- markdown-it-py 关掉原始 HTML（html=False）：Markdown 里的任何 `<script>`、`<img onerror>`
  都按文字显示。
  export 本来就把 `<` 转义了，只放行 `<sup>` / `<sub>`，这里在渲染后把转义过的上下标还原
  （只认不含尖括号的短内容）；
- 链接用 markdown-it 自带的协议检查（挡掉 javascript: / vbscript: / file: / data:），外链加
  target=_blank rel="noopener noreferrer"；
- 图片的相对路径换成控制台的 /out/ 地址，并检查解析后的文件在 output 目录里面（防 ../ 越界）；
  地址不对的图片只留 alt 文字。
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

_SUPSUB = re.compile(r"&lt;(sup|sub)&gt;((?:(?!&lt;|&gt;)[^<>]){1,40}?)&lt;/\1&gt;")


def strip_frontmatter(text: str) -> str:
    """去掉开头的 YAML frontmatter（--- … ---）。"""
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + 5 :]
    return text


def _md() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")


def _rewrite(tokens: list[Token], md_dir: Path, out_root: Path, url_prefix: str) -> None:
    for tok in tokens:
        if tok.children:
            _rewrite(tok.children, md_dir, out_root, url_prefix)
        if tok.type == "image":
            src = str(tok.attrGet("src") or "")
            tok.attrSet("src", image_url(src, md_dir, out_root, url_prefix) or "")
            tok.attrSet("loading", "lazy")
        elif tok.type == "link_open":
            href = str(tok.attrGet("href") or "")
            if urlsplit(href).scheme in ("http", "https", "mailto"):
                tok.attrSet("target", "_blank")
                tok.attrSet("rel", "noopener noreferrer")


def image_url(src: str, md_dir: Path, out_root: Path, url_prefix: str) -> str | None:
    """图片地址 → 页面上用的地址。外站 http(s) 原样保留；相对路径必须落在 output 目录里。"""
    parts = urlsplit(src)
    if parts.scheme in ("http", "https"):
        return src
    if parts.scheme or parts.netloc or src.startswith(("/", "\\")):
        return None
    try:
        target = (md_dir / unquote(parts.path)).resolve()
        rel = target.relative_to(out_root.resolve())
    except (OSError, ValueError):
        return None
    return url_prefix + quote(rel.as_posix())


def render_markdown(text: str, md_path: Path, out_root: Path, url_prefix: str = "/out/") -> str:
    md = _md()
    tokens = md.parse(strip_frontmatter(text))
    _rewrite(tokens, md_path.parent, out_root, url_prefix)
    html = md.renderer.render(tokens, md.options, {})
    # 地址被去掉的图片：markdown-it 会输出 src=""，换成只显示 alt 文字
    html = re.sub(r'<img src="" alt="([^"]*)"[^>]*>', r'<span class="img-missing">\1</span>', html)
    return _SUPSUB.sub(r"<\1>\2</\1>", html)
