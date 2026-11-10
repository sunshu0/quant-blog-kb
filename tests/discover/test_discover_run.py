import re
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
    DiscoverStats,
    SitemapEntry,
    SitemapError,
    apply_entries,
    discover_site,
    make_slug,
    prefix_matches,
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
SLUG_RE = re.compile(r"^(?:[a-z0-9_-]{1,80}-)?[0-9a-f]{8}$")


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


class FakeSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _run(
    site: SiteConfig, conn: sqlite3.Connection, fetch: FakeFetch | None = None
) -> DiscoverStats:
    return discover_site(site, conn, fetch=fetch or FakeFetch(), sleep=FakeSleep())


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = store.connect(tmp_path / "state.db")
    yield c
    c.close()


# ---- 规则 ----


def test_rules_from_repo_man_config() -> None:
    rules = DiscoverRules.from_site(config.load_site("man"))
    assert rules.host == "www.man.com"
    assert rules.sitemaps == ("https://www.man.com/sitemap.xml",)
    assert "/insights/" in rules.include_prefixes
    assert "/it/" in rules.exclude_prefixes
    assert rules.allowed_hosts == frozenset({"www.man.com"})


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


@pytest.mark.parametrize("bad", ["insights/", "", "*"])
def test_rules_reject_prefix_without_leading_slash(bad: str) -> None:
    with pytest.raises(ValueError, match="/"):
        DiscoverRules.from_site(_site(include_prefixes=[bad]))


def test_allowed_hosts_include_sitemap_host() -> None:
    rules = DiscoverRules.from_site(_site(sitemap="https://cdn.example.test/sm.xml"))
    assert rules.allowed_hosts == frozenset({"www.example.test", "cdn.example.test"})


@pytest.mark.parametrize(
    ("path", "prefix", "ok"),
    [
        ("/insights/subscription", "/insights/subscription", True),
        ("/insights/subscription/", "/insights/subscription", True),
        ("/insights/subscription/x", "/insights/subscription", True),
        ("/insights/subscription-free-article", "/insights/subscription", False),
        ("/insights/a", "/insights/", True),
        ("/insightsfoo/a", "/insights", False),
        ("/italy-and-rates", "/it/", False),
        ("/it/insights/a", "/it/", True),
        ("/anything", "/", True),
    ],
)
def test_prefix_matches_on_segment_boundary(path: str, prefix: str, ok: bool) -> None:
    assert prefix_matches(path, prefix) is ok


def test_exclude_prefix_does_not_swallow_similar_slugs() -> None:
    rules = DiscoverRules.from_site(_site())
    keep = SitemapEntry("https://www.example.test/insights/subscription-economy")
    drop = SitemapEntry("https://www.example.test/insights/subscription")
    assert to_discovered(rules, keep) is not None
    assert to_discovered(rules, drop) is None


# ---- slug ----


@pytest.mark.parametrize(
    ("loc", "readable"),
    [
        ("https://www.example.test/insights/alpha", "alpha"),
        ("https://www.example.test/insights/alpha/", "alpha"),
        ("https://www.example.test/insights/series/part-one", "series-part-one"),
        ("https://WWW.example.test/insights/alpha#frag", "alpha"),
        ("https://www.example.test/insights/a%20b", "a-b"),
        ("https://www.example.test/insights/Q1-Outlook", "q1-outlook"),
        ("https://www.example.test/insights/report.pdf", "report-pdf"),
    ],
)
def test_to_discovered_accepts(loc: str, readable: str) -> None:
    d = to_discovered(DiscoverRules.from_site(_site()), SitemapEntry(loc, "2026-01-01"))
    assert d is not None
    assert SLUG_RE.match(d.slug), d.slug
    assert d.slug.rsplit("-", 1)[0] == readable
    assert d.site == "example"
    assert d.lastmod == "2026-01-01"
    assert "#" not in d.url


@pytest.mark.parametrize(
    "loc",
    [
        "https://www.example.test/insights/",  # 列表页
        "https://www.example.test/insights",
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


def test_slug_distinguishes_paths_that_used_to_collide() -> None:
    slugs = {
        make_slug("/a/b"),
        make_slug("/a-b"),
        make_slug("/Foo"),
        make_slug("/foo"),
        make_slug("/a.b"),
        make_slug("/a-b/"),  # 和 /a-b 是同一篇，slug 相同
    }
    assert len(slugs) == 5


def test_slug_for_non_ascii_path_is_not_empty() -> None:
    raw = make_slug("/%E9%87%8F%E5%8C%96%E7%AD%96%E7%95%A5")  # 编码后的中文
    decoded = make_slug("/量化策略")
    assert raw == decoded
    assert SLUG_RE.match(raw)
    assert raw != make_slug("/另一篇")


@pytest.mark.parametrize("name", ["con", "NUL", "aux.txt", "com1", "prn.html"])
def test_slug_never_equals_windows_reserved_name(name: str) -> None:
    slug = make_slug("/" + name)
    base = slug.split(".")[0].lower()
    assert base not in {"con", "nul", "aux", "prn", "com1", "lpt1"}
    assert "." not in slug


def test_slug_length_is_bounded() -> None:
    slug = make_slug("/" + "very-long-title-" * 40)
    assert len(slug) <= 80 + 1 + 8
    assert SLUG_RE.match(slug)


def test_slug_cannot_escape_directory() -> None:
    assert make_slug("..") == make_slug("..")  # 稳定
    for rest in ("../../etc/passwd", "..", "a/b/c", "%2e%2e/x", "a\\b"):
        slug = make_slug(rest)
        assert "/" not in slug
        assert "\\" not in slug
        assert not slug.startswith(".")
        assert SLUG_RE.match(slug), slug
    assert make_slug("") == ""
    assert make_slug("/") == ""


def test_apply_entries_detects_slug_conflict(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    import qbkb.discover.run as run_mod

    monkeypatch.setattr(run_mod, "make_slug", lambda rest: "same-00000000")
    rules = DiscoverRules.from_site(_site())
    a = "https://www.example.test/insights/a"
    b = "https://www.example.test/insights/b"
    stats = apply_entries(conn, rules, [SitemapEntry(b), SitemapEntry(a)])
    assert (stats.new, stats.conflicts) == (1, 1)
    assert store.get_article(conn, a) is not None  # 按 URL 排序，总是保留同一个
    assert store.get_article(conn, b) is None
    assert any("slug 冲突" in e and b in e for e in stats.errors)


def test_trailing_slash_variant_is_a_conflict_not_an_overwrite(conn: sqlite3.Connection) -> None:
    rules = DiscoverRules.from_site(_site())
    stats = apply_entries(
        conn,
        rules,
        [
            SitemapEntry("https://www.example.test/insights/alpha"),
            SitemapEntry("https://www.example.test/insights/alpha/"),
        ],
    )
    assert (stats.new, stats.conflicts) == (1, 1)


# ---- 整体流程 ----


def test_discover_site_walks_index(conn: sqlite3.Connection) -> None:
    fetch = FakeFetch()
    sleep = FakeSleep()
    stats = discover_site(_site(), conn, fetch=fetch, sleep=sleep)
    # 外站子 sitemap 不跟随；重复的子 sitemap 只抓一次
    assert fetch.calls == list(PAGES)
    assert stats.sitemaps_fetched == 3
    # 第一份之前不等，之后每份之前等 2 秒
    assert sleep.calls == [2.0, 2.0]
    assert stats.errors == []
    assert (stats.new, stats.updated, stats.unchanged, stats.conflicts) == (5, 0, 0, 0)
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
    assert beta["slug"].startswith("beta-carry-")


def test_rerun_is_idempotent(conn: sqlite3.Connection) -> None:
    _run(_site(), conn)
    stats = _run(_site(), conn)
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
        _run(_site(decision), conn, fetch)
    assert fetch.calls == []


def test_nested_failure_is_recorded_not_fatal(conn: sqlite3.Connection) -> None:
    pages = {k: v for k, v in PAGES.items() if "pages" not in k}
    stats = _run(_site(), conn, FakeFetch(pages))
    assert len(stats.errors) == 1
    assert "sitemap-pages.xml" in stats.errors[0]
    assert stats.new == 3


def test_nested_corrupt_gzip_is_recorded_not_fatal(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.xml.gz"
    bad.write_bytes(bytes((0x1F, 0x8B)) + b"\x08\x00garbage")
    pages = {**PAGES, "https://www.example.test/sitemap-pages.xml": str(bad)}
    stats = _run(_site(), conn, FakeFetch(pages))
    assert any("gzip" in e for e in stats.errors)
    assert stats.new == 3


def test_root_failure_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(httpx.HTTPError):
        _run(_site(), conn, FakeFetch({}))


def test_root_not_a_sitemap_raises(conn: sqlite3.Connection) -> None:
    with pytest.raises(SitemapError):
        discover_site(_site(), conn, fetch=lambda _url: b"<html></html>", sleep=FakeSleep())


def test_max_sitemaps_limits_requests(conn: sqlite3.Connection) -> None:
    fetch = FakeFetch()
    stats = _run(_site(max_sitemaps=2), conn, fetch)
    assert len(fetch.calls) == 2
    assert stats.sitemaps_fetched == 2
    assert any("max_sitemaps" in e for e in stats.errors)
