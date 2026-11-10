import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from qbkb import config, store
from qbkb.config import ComplianceError, SiteConfig
from qbkb.discover import (
    DiscoverRules,
    SitemapEntry,
    SitemapError,
    apply_entries,
    discover_site,
    make_slug,
    to_discovered,
)
from qbkb.models import Status

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
INDEX = "https://www.example.test/sitemap.xml"
PAGES = {
    INDEX: "sitemap_index.xml",
    "https://www.example.test/sitemap-insights.xml": "sitemap_insights.xml",
    "https://www.example.test/sitemap-pages.xml": "sitemap_pages_no_ns.xml",
}


def _site(decision: str = "allowed", **discover: Any) -> SiteConfig:
    section: dict[str, Any] = {
        "sitemap": INDEX,
        "include_prefixes": ["/insights/"],
        "exclude_prefixes": ["/it/", "/es/", "/insights/subscription"],
    }
    section.update(discover)
    raw = {
        "compliance": {"decision": decision},
        "base_url": "https://www.example.test",
        "discover": section,
    }
    return SiteConfig(name="example", compliance_decision=decision, raw=raw)


class FakeFetch:
    """离线的 sitemap 下载：URL → fixtures 文件；记录请求过哪些 URL。"""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = PAGES if pages is None else pages
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        name = self.pages.get(url)
        if name is None:
            raise httpx.HTTPStatusError(
                "404",
                request=httpx.Request("GET", url),
                response=httpx.Response(404, request=httpx.Request("GET", url)),
            )
        return (FIXTURES / name).read_bytes()


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


def test_rules_from_repo_man_config() -> None:
    rules = DiscoverRules.from_site(config.load_site("man"))
    assert rules.host == "www.man.com"
    assert rules.sitemaps == ("https://www.man.com/sitemap.xml",)
    assert "/insights/" in rules.include_prefixes
    assert "/it/" in rules.exclude_prefixes


def test_rules_defaults_when_section_missing() -> None:
    site = SiteConfig("x", "allowed", {"base_url": "https://www.example.test/"})
    rules = DiscoverRules.from_site(site)
    assert rules.sitemaps == ("https://www.example.test/sitemap.xml",)
    assert rules.include_prefixes == ("/",)
    assert rules.exclude_prefixes == ()
    assert rules.max_sitemaps == 50


def test_rules_require_base_url() -> None:
    with pytest.raises(ValueError):
        DiscoverRules.from_site(SiteConfig("x", "allowed", {}))


@pytest.mark.parametrize(
    ("loc", "slug"),
    [
        ("https://www.example.test/insights/alpha", "alpha"),
        ("https://www.example.test/insights/alpha/", "alpha"),
        ("https://www.example.test/insights/series/part-one", "series-part-one"),
        ("https://WWW.example.test/insights/alpha#frag", "alpha"),
        ("https://www.example.test/insights/a%20b", "a-20b"),
    ],
)
def test_to_discovered_accepts(loc: str, slug: str) -> None:
    d = to_discovered(DiscoverRules.from_site(_site()), SitemapEntry(loc, "2026-01-01"))
    assert d is not None
    assert d.slug == slug
    assert d.site == "example"
    assert d.lastmod == "2026-01-01"
    assert "#" not in d.url


@pytest.mark.parametrize(
    "loc",
    [
        "https://www.example.test/insights/",  # 列表页
        "https://www.example.test/insights/subscription",  # 排除前缀
        "https://www.example.test/it/insights/alpha",  # 语言版本
        "https://www.example.test/es/insights/alpha",
        "https://www.example.test/about",  # 不在白名单
        "https://other.test/insights/alpha",  # 外站
        "ftp://www.example.test/insights/alpha",
    ],
)
def test_to_discovered_rejects(loc: str) -> None:
    assert to_discovered(DiscoverRules.from_site(_site()), SitemapEntry(loc)) is None


def test_slug_cannot_escape_directory() -> None:
    assert make_slug("../../etc/passwd") == "etc-passwd"
    assert make_slug("..") == ""
    assert "/" not in make_slug("a/b/c")


def test_discover_site_walks_index(conn: sqlite3.Connection) -> None:
    fetch = FakeFetch()
    stats = discover_site(_site(), conn, fetch=fetch)
    # 外站子 sitemap 不跟随；重复的子 sitemap 只抓一次
    assert fetch.calls == list(PAGES)
    assert stats.sitemaps_fetched == 3
    assert stats.errors == []
    assert (stats.new, stats.updated, stats.unchanged) == (5, 0, 0)
    # 7 条规则过滤 + 1 条重复 URL
    assert stats.filtered == 8
    urls = store.list_by_status(conn, Status.DISCOVERED, "example")
    assert urls == [
        "https://www.example.test/insights/alpha-signals",
        "https://www.example.test/insights/beta-carry",
        "https://www.example.test/insights/delta-volatility",
        "https://www.example.test/insights/report.pdf",
        "https://www.example.test/insights/series/part-one",
    ]
    beta = store.get_article(conn, "https://www.example.test/insights/beta-carry")
    assert beta is not None
    assert beta["lastmod"] == "2026-01-04"  # 重复时保留较新的 lastmod
    assert beta["slug"] == "beta-carry"


def test_rerun_is_idempotent(conn: sqlite3.Connection) -> None:
    discover_site(_site(), conn, fetch=FakeFetch())
    stats = discover_site(_site(), conn, fetch=FakeFetch())
    assert (stats.new, stats.updated, stats.unchanged) == (0, 0, 5)


def test_changed_lastmod_counts_as_updated(conn: sqlite3.Connection) -> None:
    rules = DiscoverRules.from_site(_site())
    url = "https://www.example.test/insights/alpha"
    apply_entries(conn, rules, [SitemapEntry(url, "2026-01-01")])
    store.set_status(conn, url, Status.FETCHED, etag='"v1"')
    stats = apply_entries(conn, rules, [SitemapEntry(url, "2026-02-01")])
    assert (stats.new, stats.updated, stats.unchanged) == (0, 1, 0)
    row = store.get_article(conn, url)
    assert row is not None
    assert row["status"] == Status.DISCOVERED


@pytest.mark.parametrize("decision", ["permission_needed", "blocked"])
def test_compliance_gate_runs_before_any_request(conn: sqlite3.Connection, decision: str) -> None:
    fetch = FakeFetch()
    with pytest.raises(ComplianceError):
        discover_site(_site(decision), conn, fetch=fetch)
    assert fetch.calls == []


def test_nested_failure_is_recorded_not_fatal(conn: sqlite3.Connection) -> None:
    pages = {k: v for k, v in PAGES.items() if "pages" not in k}
    stats = discover_site(_site(), conn, fetch=FakeFetch(pages))
    assert len(stats.errors) == 1
    assert "sitemap-pages.xml" in stats.errors[0]
    assert stats.new == 3


def test_root_failure_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(httpx.HTTPError):
        discover_site(_site(), conn, fetch=FakeFetch({}))


def test_root_not_a_sitemap_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(SitemapError):
        discover_site(_site(), conn, fetch=lambda _url: b"<html></html>")


def test_max_sitemaps_limits_requests(conn: sqlite3.Connection) -> None:
    fetch = FakeFetch()
    stats = discover_site(_site(max_sitemaps=2), conn, fetch=fetch)
    assert len(fetch.calls) == 2
    assert stats.sitemaps_fetched == 2
    assert any("max_sitemaps" in e for e in stats.errors)
