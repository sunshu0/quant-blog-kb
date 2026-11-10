"""翻译（可插拔后端、术语表、段落级翻译记忆）。Phase 1 C 线负责。"""

from qbkb.translate.base import FREE_MONTHLY_CHARS, SupportsContext, count_chars
from qbkb.translate.deepl_backend import DeepLTranslator
from qbkb.translate.fake import FakeTranslator
from qbkb.translate.glossary import Glossary, load_glossary
from qbkb.translate.pipeline import (
    TranslateStats,
    TranslationStopped,
    estimate_chars,
    translate_article,
)
from qbkb.translate.structure import same_structure

__all__ = [
    "FREE_MONTHLY_CHARS",
    "DeepLTranslator",
    "FakeTranslator",
    "Glossary",
    "SupportsContext",
    "TranslateStats",
    "TranslationStopped",
    "count_chars",
    "estimate_chars",
    "load_glossary",
    "same_structure",
    "translate_article",
]
