"""翻译（可插拔后端、术语表、段落级翻译记忆）。Phase 1 C 线负责。"""

from qbkb.translate.base import (
    FREE_MONTHLY_CHARS,
    MAX_REQUEST_CHARS,
    SegmentTooLargeError,
    SupportsContext,
    TranslatorUnavailableError,
    count_chars,
)
from qbkb.translate.deepl_backend import DeepLTranslator
from qbkb.translate.fake import FakeTranslator
from qbkb.translate.glossary import Glossary, load_glossary
from qbkb.translate.pipeline import (
    REVIEW_PREFIX,
    BackendStopped,
    QuotaStopped,
    TranslateStats,
    TranslationStopped,
    estimate_chars,
    glossary_upgrade_cost,
    needs_review,
    translate_article,
)
from qbkb.translate.structure import same_structure

__all__ = [
    "FREE_MONTHLY_CHARS",
    "MAX_REQUEST_CHARS",
    "REVIEW_PREFIX",
    "BackendStopped",
    "DeepLTranslator",
    "FakeTranslator",
    "Glossary",
    "QuotaStopped",
    "SegmentTooLargeError",
    "SupportsContext",
    "TranslateStats",
    "TranslationStopped",
    "TranslatorUnavailableError",
    "count_chars",
    "estimate_chars",
    "glossary_upgrade_cost",
    "load_glossary",
    "needs_review",
    "same_structure",
    "translate_article",
]
