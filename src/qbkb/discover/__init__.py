"""URL 发现（sitemap → RSS → 列表页 API → 无头浏览器）。Phase 1 A 线负责。

目前只实现 sitemap（含 sitemap index）。
"""

from qbkb.discover.run import (
    DiscoverRules,
    DiscoverStats,
    apply_entries,
    collect_entries,
    discover_site,
    make_slug,
    to_discovered,
)
from qbkb.discover.sitemap import Sitemap, SitemapEntry, SitemapError, parse_sitemap

__all__ = [
    "DiscoverRules",
    "DiscoverStats",
    "Sitemap",
    "SitemapEntry",
    "SitemapError",
    "apply_entries",
    "collect_entries",
    "discover_site",
    "make_slug",
    "parse_sitemap",
    "to_discovered",
]
