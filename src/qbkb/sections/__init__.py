"""按栏目采集（design.md 4.9）：栏目定义、页面判定、状态推进、栏目调研。"""

from qbkb.sections.apply import (
    ApplyStats,
    NewSection,
    apply_sections,
    new_sections,
    translatable,
)
from qbkb.sections.rules import (
    PageSection,
    Section,
    SectionRules,
    load_section_rules,
    normalize_key,
    page_section,
)

__all__ = [
    "ApplyStats",
    "NewSection",
    "PageSection",
    "Section",
    "SectionRules",
    "apply_sections",
    "load_section_rules",
    "new_sections",
    "normalize_key",
    "page_section",
    "translatable",
]
