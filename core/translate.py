"""Detect the language of a listing description and translate to English.

Uses `langdetect` for ISO-639-1 detection and `deep-translator` (free Google
Translate web endpoint, no API key) for translation. Both calls swallow
errors and return None — translation is a nice-to-have, never a blocker.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)


def detect_language(text: str) -> Optional[str]:
    if not text or len(text.strip()) < 20:
        return None
    try:
        from langdetect import detect, DetectorFactory  # type: ignore
        DetectorFactory.seed = 0  # deterministic across runs
        return detect(text)
    except Exception as exc:
        log.debug("language detection failed: %s", exc)
        return None


def translate_to_english(text: str, source: Optional[str] = None) -> Optional[str]:
    """Return the English translation of `text`, or None on failure.

    `source` is an ISO-639-1 hint; pass None or 'auto' to let the translator
    detect. We intentionally cap input length — Google's free endpoint has a
    soft limit around 5K chars and the descriptions we store are capped at
    1000 anyway.
    """
    if not text:
        return None
    if source and source.lower() in ("en", "auto"):
        # 'auto' is fine to pass through; 'en' would short-circuit but we
        # let the caller decide whether to bother calling.
        pass
    try:
        from deep_translator import GoogleTranslator  # type: ignore
        return GoogleTranslator(source=source or "auto", target="en").translate(text[:4500])
    except Exception as exc:
        log.debug("translation failed: %s", exc)
        return None


def detect_and_translate(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Convenience: returns (language_code, english_translation).

    `english_translation` is None when the text is already English or when
    translation failed. The caller can persist the detected language even
    when translation is None — that's why we return both.
    """
    lang = detect_language(text)
    if not lang or lang == "en":
        return lang, None
    translated = translate_to_english(text, source=lang)
    return lang, translated
