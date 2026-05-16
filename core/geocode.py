"""City / location string → ISO country code via Nominatim, with disk cache.

Used as a fallback when a scraper extracts a city name but no `country_code`.
Nominatim is OpenStreetMap's free geocoder — no API key, ~1 req/sec rate
cap per TOS, identifying User-Agent required. Cache file is in `.cache/`
(gitignored) so repeat lookups don't hit the network and we stay polite.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "find_my_ride/1.0 (classic-car listings aggregator)"

_CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "geocode.json"
_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_cache: Optional[dict] = None
_last_request_at = 0.0


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with _CACHE_PATH.open() as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        with _CACHE_PATH.open("w") as f:
            json.dump(_cache, f, indent=2, sort_keys=True, ensure_ascii=False)
    except Exception as exc:
        log.debug("geocode cache save failed: %s", exc)


def lookup_country(location: Optional[str]) -> Optional[str]:
    """Resolve a free-form location string to an ISO 3166-1 alpha-2 code.

    Cached on disk indefinitely. Misses (None results) are also cached so a
    bad string doesn't hit Nominatim every scrape. Returns None when the
    location can't be resolved or Nominatim is unreachable.
    """
    if not location:
        return None
    key = location.strip()
    if not key:
        return None
    cache = _load_cache()
    if key in cache:
        return cache[key] or None  # may be cached as ""

    global _last_request_at
    with _lock:
        # Nominatim TOS: max 1 request/sec. Cheap to enforce.
        delay = 1.05 - (time.time() - _last_request_at)
        if delay > 0:
            time.sleep(delay)
        try:
            resp = requests.get(
                _NOMINATIM_URL,
                params={"q": key, "format": "json", "limit": 1, "addressdetails": 1},
                headers={"User-Agent": _USER_AGENT},
                timeout=10,
            )
            _last_request_at = time.time()
            if resp.status_code != 200:
                return None
            data = resp.json()
        except Exception as exc:
            log.debug("nominatim lookup failed for %r: %s", key, exc)
            return None

    iso = None
    if data:
        addr = (data[0] or {}).get("address") or {}
        iso = (addr.get("country_code") or "").upper() or None

    cache[key] = iso or ""
    _save_cache()
    return iso
