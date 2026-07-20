from __future__ import annotations

import logging
from typing import Dict, Optional

import requests

log = logging.getLogger(__name__)

_RATE_CACHE: Dict[str, float] = {}
_FETCH_FAILED = False


def _fetch_usd_rates() -> Dict[str, float]:
    """Fetch current USD exchange rates (free, no key needed). Cached per process run."""
    global _FETCH_FAILED
    if _RATE_CACHE or _FETCH_FAILED:
        return _RATE_CACHE
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        resp.raise_for_status()
        rates = resp.json().get("rates", {})
        _RATE_CACHE.update(rates)
        log.debug(
            "Exchange rates fetched: EUR=%.4f GBP=%.4f",
            rates.get("EUR", 0), rates.get("GBP", 0),
        )
    except Exception as exc:
        log.warning("Could not fetch exchange rates: %s", exc)
        _FETCH_FAILED = True
    return _RATE_CACHE


def rates_available() -> bool:
    """True if we have working exchange rates this process."""
    _fetch_usd_rates()
    return bool(_RATE_CACHE)


def usd_value(price_value: Optional[int], currency: Optional[str]) -> Optional[float]:
    """Convert a price (minor units, e.g. pence) + ISO 4217 currency to a USD float.

    Returns None on missing inputs or unavailable rate. Callers must distinguish
    "no rate available" from "value is zero" before applying a price floor.
    """
    if price_value is None or not currency:
        return None
    rates = _fetch_usd_rates()
    rate = rates.get(currency)
    if not rate:
        return None
    try:
        return (price_value / 100) / rate
    except Exception:
        return None


def to_usd_str(price_value: Optional[int], currency: Optional[str]) -> Optional[str]:
    """Convert a price to a `$12,345` USD display string, or None."""
    v = usd_value(price_value, currency)
    if v is None:
        return None
    return f"${v:,.0f}"


# ISO 4217 codes we surface in the UI - the currencies used across the
# country allowlist (eurozone + GB + the non-euro EU/EEA/CH markets). Any
# code here must be selectable in the ListingCard currency dropdown and
# accepted by the /api/override validation. usd_value() converts all of them
# (open.er-api.com returns every ISO rate); symbols below are display-only,
# with an ISO-code fallback for the ones without a common single glyph.
SUPPORTED_CURRENCIES = (
    "EUR", "GBP", "USD", "DKK", "SEK", "NOK", "PLN",
    "CHF", "CZK", "HUF", "RON", "BGN", "ISK",
)

_CURRENCY_SYMBOLS = {
    "EUR": "€", "GBP": "£", "USD": "$",
    "DKK": "kr", "SEK": "kr", "NOK": "kr", "ISK": "kr",
    "PLN": "zł", "CZK": "Kč", "HUF": "Ft", "RON": "lei", "BGN": "лв",
    # CHF has no distinct glyph in common use; ISO code reads fine.
}


def format_price(price_value: Optional[int], currency: Optional[str]) -> Optional[str]:
    """Render minor-units + ISO code as a symbol-prefixed display string.

    `format_price(2195000, 'GBP')` → `'£21,950'`. Returns None when either
    input is missing; callers should fall back to whatever the scraper
    originally stored as raw text in that case.
    """
    if price_value is None or not currency:
        return None
    symbol = _CURRENCY_SYMBOLS.get(currency, currency)
    return f"{symbol}{price_value / 100:,.0f}"
