"""端到端测试用的构造站点：sitemap、文章页（照 man.com 的模板结构，正文是自己写的句子）、
图片，以及指向它的一套临时配置。不含任何真实文章内容。"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from qbkb.config import CONFIG_DIR

SITE = "local"
PNG = b"\x89PNG\r\n\x1a\nconstructed"


def _sentences(topic: str, n: int) -> list[str]:
    """n 段自己写的句子，每段约 40 个单词。"""
    return [
        f"<p>Paragraph {i} about {topic}: a trend following rule buys what has been rising "
        f"and sells what has been falling, and this made-up example walks through step {i} "
        "of the idea with invented numbers so the pipeline has enough words to work with "
        "in an offline test.</p>"
        for i in range(n)
    ]


@dataclass(frozen=True, slots=True)
class Page:
    path: str
    title: str
    date: str  # 页面上显示的日期，例如 "7 May 2024"
    paras: tuple[str, ...]
    image: str | None = None


PAGES = (
    Page(
        "/insights/carry-basics",
        "Carry Basics Explained",
        "7 May 2024",
        tuple(_sentences("carry", 10)),
        "/files/carry-fig1.png",
    ),
    Page(
        "/insights/momentum-method",
        "Building a Momentum Signal",
        "12 June 2023",
        tuple(_sentences("momentum", 9)),
        "/files/momentum-fig1.png",
    ),
    Page(
        "/insights/climate-note", "A Climate Note", "1 March 2024", tuple(_sentences("weather", 9))
    ),
    Page(
        "/insights/a-moment-in-macro/rates-week",
        "Rates This Week",
        "2 February 2025",
        tuple(_sentences("rates", 9)),
    ),
    Page("/insights/short-clip", "A Short Clip", "3 January 2025", tuple(_sentences("clips", 1))),
)


def page_html(p: Page) -> str:
    img = f'<img src="{p.image}" alt="Constructed chart">' if p.image else ""
    body = "\n".join(p.paras[:2]) + img + "\n".join(p.paras[2:])
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{p.title} | Example</title>
<meta property="og:title" content="{p.title} | Man Group">
</head><body>
<header><nav><a href="/insights">Insights</a></nav></header>
<main>
<div id="article-tagline-text"><p>ARTICLE | 5 MIN</p></div>
<div id="article-title-text"><h1>{p.title}</h1></div>
<div id="article-date-text"><p>{p.date}</p></div>
<div id="article-authors"><a href="/people/x"><span class="fw-600">Test Author</span></a></div>
<div id="article-body-text"><div class="digital-article">
<h2>Overview</h2>
{body}
</div></div>
<div id="insights-important-information-section">
<h2 id="insights-important-information-section-title">Important information</h2>
<div class="accordion-body">
<p>This constructed page is a test fixture and is not investment material.</p></div>
</div>
</main></body></html>
"""


def sitemap_xml(base: str, pages: tuple[Page, ...] = PAGES) -> str:
    urls = "".join(
        f"<url><loc>{base}{p.path}</loc><lastmod>2026-01-0{i + 1}</lastmod></url>"
        for i, p in enumerate(pages)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    )


def responses(base: str, pages: tuple[Page, ...] = PAGES) -> dict[str, tuple[str, bytes]]:
    """路径 → (Content-Type, 内容)。"""
    out: dict[str, tuple[str, bytes]] = {
        "/robots.txt": ("text/plain", b"User-agent: *\nDisallow:\n"),
        "/sitemap.xml": ("application/xml", sitemap_xml(base, pages).encode("utf-8")),
    }
    for p in pages:
        out[p.path] = ("text/html; charset=utf-8", page_html(p).encode("utf-8"))
        if p.image:
            out[p.image] = ("image/png", PNG)
    return out


def make_config(root: Path, base: str) -> Path:
    """仓库 config/ 的副本，加一个指向 base 的站点 local（规则照抄 man.yaml）。"""
    cfg = root / "config"
    shutil.copytree(CONFIG_DIR, cfg, ignore=shutil.ignore_patterns("sites"))
    man = yaml.safe_load((CONFIG_DIR / "sites" / "man.yaml").read_text(encoding="utf-8"))
    man["base_url"] = base
    man["firm"] = "Example Capital"
    man["discover"]["sitemap"] = f"{base}/sitemap.xml"
    man["compliance"] = {"decision": "allowed", "checked_at": "2026-11-11"}
    (cfg / "sites").mkdir()
    (cfg / "sites" / f"{SITE}.yaml").write_text(
        yaml.safe_dump(man, allow_unicode=True), encoding="utf-8"
    )
    filters = yaml.safe_load((cfg / "filters.yaml").read_text(encoding="utf-8"))
    filters["sites"][SITE] = filters["sites"]["man"]
    (cfg / "filters.yaml").write_text(yaml.safe_dump(filters, allow_unicode=True), encoding="utf-8")
    return cfg


@dataclass(slots=True)
class Hits:
    paths: list[str] = field(default_factory=list[str])
    agents: list[str] = field(default_factory=list[str])


@contextmanager
def serve(pages: tuple[Page, ...] = PAGES) -> Iterator[tuple[str, Hits]]:
    """本机 HTTP 服务（127.0.0.1，临时端口），返回 (base_url, 请求记录)。"""
    hits = Hits()
    table: dict[str, tuple[str, bytes]] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.paths.append(self.path)
            hits.agents.append(self.headers.get("User-Agent", ""))
            found = table.get(self.path)
            status, (ctype, body) = (200, found) if found else (404, ("text/plain", b"nope"))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    table.update(responses(base, pages))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, hits
    finally:
        srv.shutdown()
        srv.server_close()
