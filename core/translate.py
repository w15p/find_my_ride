"""Detect the language of a listing description and translate to English.

Uses `langdetect` for ISO-639-1 detection and `deep-translator` (free Google
Translate web endpoint, no API key) for translation. Both calls swallow
errors and return None — translation is a nice-to-have, never a blocker.
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple

log = logging.getLogger(__name__)


# ISO 3166 country -> ISO 639-1 language. Only countries with a single
# dominant listing-language, so we can use this as a strong prior instead
# of langdetect (which is unreliable on car-listing text: short strings,
# UPPERCASE, similar Romance languages, proper nouns and numbers). Multi-
# language countries (BE, CH, LU, MT, CY) intentionally fall through to
# text-based detection.
_COUNTRY_TO_LANG = {
    "IT": "it", "DE": "de", "AT": "de",
    "FR": "fr", "ES": "es", "PT": "pt", "NL": "nl",
    "DK": "da", "SE": "sv", "FI": "fi", "NO": "no", "IS": "is",
    "PL": "pl", "CZ": "cs", "SK": "sk", "HU": "hu", "RO": "ro",
    "BG": "bg", "GR": "el", "HR": "hr", "SI": "sl",
    "EE": "et", "LV": "lv", "LT": "lt",
    "IE": "en", "GB": "en",
}


def detect_language(text: str, country_code: Optional[str] = None) -> Optional[str]:
    """Best-effort ISO-639-1 label for `text`.

    Prefers a country_code-derived language for single-language countries -
    that's much more accurate than langdetect on the actual descriptions we
    see (short + uppercase + proper-noun-heavy). Falls back to langdetect
    for multi-language countries or when no country is provided.
    """
    if country_code:
        mapped = _COUNTRY_TO_LANG.get(country_code.upper())
        if mapped:
            return mapped
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


def detect_and_translate(
    text: str, country_code: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Convenience: returns (language_code, english_translation).

    `english_translation` is None when the text is already English or when
    translation failed. The caller can persist the detected language even
    when translation is None - that's why we return both. Pass country_code
    when known - it's a strong prior for the language label AND lets us skip
    langdetect entirely in single-language countries.

    We deliberately pass source="auto" to Google Translate regardless of our
    own detection: Google's detector is far more accurate than langdetect,
    especially on the uppercase-Italian-mistaken-for-Portuguese class of
    failure. The `lang` we return is used only for the UI label
    ("translated from Italian").
    """
    lang = detect_language(text, country_code=country_code)
    if not lang or lang == "en":
        return lang, None
    translated = translate_to_english(text, source="auto")
    return lang, translated
