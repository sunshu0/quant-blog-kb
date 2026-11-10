"""抓取结果的类型。不依赖 Scrapy，状态统计和 discover 都能直接导入。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Kind(StrEnum):
    FETCHED = "fetched"
    NOT_MODIFIED = "not_modified"
    PDF = "pdf"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Outcome:
    kind: Kind
    http_status: int | None
    reason: str | None = None
    etag: str | None = None
