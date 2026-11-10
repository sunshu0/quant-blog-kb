"""URL 发现（sitemap → RSS → 列表页 API → 无头浏览器）。Phase 1 A 线负责。

目前只实现 sitemap（含 sitemap index）。
"""

from qbkb.discover.run import (
    DiscoverRules,
    DiscoverStats,
    apply_entries,
    collect_entries,
    discover_site,
    http_get,
    make_slug,
    prefix_matches,
    to_discovered,
)
from qbkb.discover.sitemap import (
    MAX_SITEMAP_BYTES,
    Sitemap,
    SitemapEntry,
    SitemapError,
    parse_sitemap,
)

__all__ = [
    "MAX_SITEMAP_BYTES",
    "DiscoverRules",
    "DiscoverStats",
    "Sitemap",
    "SitemapEntry",
    "SitemapError",
    "apply_entries",
    "collect_entries",
    "discover_site",
    "http_get",
    "make_slug",
    "parse_sitemap",
    "prefix_matches",
    "to_discovered",
]
