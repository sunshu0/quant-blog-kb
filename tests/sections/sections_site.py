"""栏目测试共用：构造的站点配置和页面（照 man.com 的模板结构，内容是自己写的）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from qbkb.config import SiteConfig

BASE = "https://blog.example.test"

SECTIONS: dict[str, Any] = {
    "page_key": {
        "selector": "#article-tagline-text p",
        "pattern": (
            r"^\s*(?P<type>[^|]+?)\s*(?:\|\s*(?P<length>\d+\s*MINS?)\s*)?"
            r"(?:\|\s*(?P<series>[^|]+?)\s*)?$"
        ),
        "parts": ["type", "series"],
    },
    "cross_check": {
        "pattern": r'"contentType"\s*:\s*"([^"]+)"',
        "part": "type",
        "label": "dataLayer contentType",
        "equivalent": {"PODCAST": ["Article"]},
    },
    "list": [
        {
            "id": "macro",
            "name": "Macro Blog",
            "url_prefix": "/insights/macro-blog",
            "collect": False,
        },
        {"id": "article", "name": "Articles", "key": "ARTICLE", "collect": True},
        {
            "id": "weekly",
            "name": "Weekly Notes",
            "key": "ARTICLE | WEEKLY NOTES",
            "collect": False,
            "estimate": ["^weekly-notes"],
        },
        {"id": "video", "name": "Videos", "key": "VIDEO", "collect": False},
    ],
}


def site_raw(sections: dict[str, Any] | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "compliance": {"decision": "allowed"},
        "base_url": BASE,
        "discover": {"sitemap": f"{BASE}/sitemap.xml", "include_prefixes": ["/insights/"]},
        "extract": {
            "min_chars": 100,
            "templates": [
                {
                    "name": "insight",
                    "url_prefix": "/insights/",
                    "require": "#article-body-text",
                    "title": "#article-title-text h1",
                    "published": "#article-date-text p",
                    "author": "#article-authors a span.fw-600",
                    "body": ["#article-body-text .digital-article"],
                }
            ],
            "author_fallback": ["#blog-author-grid .team-card h4"],
        },
    }
    if sections is not None:
        raw["sections"] = sections
    return raw


def make_site(sections: dict[str, Any] | None = None, name: str = "ex") -> SiteConfig:
    return SiteConfig(name=name, compliance_decision="allowed", raw=site_raw(sections))


def write_config(root: Path, sections: dict[str, Any] | None = None, name: str = "ex") -> Path:
    cfg = root / "config"
    (cfg / "sites").mkdir(parents=True)
    (cfg / "sites" / f"{name}.yaml").write_text(
        yaml.safe_dump(site_raw(sections), allow_unicode=True), encoding="utf-8"
    )
    return cfg


def page(
    title: str,
    tagline: str | None = "ARTICLE | 5 MIN",
    content_type: str | None = None,
    words: int = 60,
) -> str:
    tag = f'<div id="article-tagline-text"><p>{tagline}</p></div>' if tagline is not None else ""
    data = (
        f'<script>dataLayer.push({{"contentType":"{content_type}"}});</script>'
        if content_type
        else ""
    )
    body = " ".join(f"word{i}" for i in range(words))
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title>{data}</head><body><main>
{tag}
<div id="article-title-text"><h1>{title}</h1></div>
<div id="article-date-text"><p>7 May 2024</p></div>
<div id="article-authors"><a href="/p"><span class="fw-600">Test Author</span></a></div>
<div id="article-body-text"><div class="digital-article"><p>{body}</p></div></div>
</main></body></html>"""
