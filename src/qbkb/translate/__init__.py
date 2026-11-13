"""翻译（可插拔后端、术语表、段落级翻译记忆）。Phase 1 C 线负责。"""

from qbkb.translate.azure_backend import AzureTranslator
from qbkb.translate.base import (
    FREE_MONTHLY_CHARS,
    MAX_REQUEST_CHARS,
    ArticleTranslationError,
    BackendAuthError,
    SegmentTooLargeError,
    TranslatorUnavailableError,
    count_chars,
)
from qbkb.translate.deepl_backend import DeepLTranslator, GlossaryLimitError
from qbkb.translate.fake import FakeTranslator
from qbkb.translate.fixed import FIXED_FILE
from qbkb.translate.glossary import Glossary, load_glossary
from qbkb.translate.pipeline import (
    BackendStopped,
    FixedTranslationError,
    QuotaStopped,
    TranslateStats,
    TranslationStopped,
    backfill_titles,
    estimate_chars,
    fixed_translation_errors,
    glossary_upgrade_cost,
    lookup_translation,
    missing_titles,
    needs_review,
    title_unit,
    translate_article,
    translated_title,
)
from qbkb.translate.structure import same_structure

__all__ = [
    "FREE_MONTHLY_CHARS",
    "MAX_REQUEST_CHARS",
    "ArticleTranslationError",
    "AzureTranslator",
    "BackendAuthError",
    "BackendStopped",
    "DeepLTranslator",
    "FakeTranslator",
    "FIXED_FILE",
    "FixedTranslationError",
    "GlossaryLimitError",
    "Glossary",
    "QuotaStopped",
    "SegmentTooLargeError",
    "TranslateStats",
    "TranslationStopped",
    "TranslatorUnavailableError",
    "count_chars",
    "estimate_chars",
    "fixed_translation_errors",
    "glossary_upgrade_cost",
    "load_glossary",
    "backfill_titles",
    "lookup_translation",
    "missing_titles",
    "needs_review",
    "same_structure",
    "title_unit",
    "translate_article",
    "translated_title",
]
