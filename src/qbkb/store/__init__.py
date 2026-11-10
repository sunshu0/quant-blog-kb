"""SQLite 状态库。三条线共用，改动走单独的 PR。"""

from qbkb.store.db import (
    TM_OK,
    TM_REJECTED,
    TmEntry,
    TmQuality,
    connect,
    get_article,
    get_segment_hashes,
    get_segments,
    list_by_status,
    record_failure,
    replace_segments,
    set_status,
    tm_get,
    tm_lookup,
    tm_put,
    upsert_discovered,
)

__all__ = [
    "TM_OK",
    "TM_REJECTED",
    "TmEntry",
    "TmQuality",
    "connect",
    "get_article",
    "get_segments",
    "get_segment_hashes",
    "list_by_status",
    "record_failure",
    "replace_segments",
    "set_status",
    "tm_get",
    "tm_lookup",
    "tm_put",
    "upsert_discovered",
]
