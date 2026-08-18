"""Stable per-listing identity derived from a URL.

A single advert reaches us through more than one URL. AutoScout24 serves the
same car from every locale domain and sometimes injects a category token into
the slug; Facebook Marketplace differs only by a trailing slash; the same eBay
item number resolves on every national eBay domain. Reaching the same advert
by a second route created a second row, so it appeared twice in the grid.

Every one of those URLs carries the site's own immutable id for the advert.
Extracting it gives an identity that survives slug, domain and query-string
changes, which phash and fingerprint matching cannot: a re-encoded image is a
different phash, and an aggregator that does not report a seller location
produces a different fingerprint.

Returns None for a URL whose site has no recognised id pattern. That is not an
error - those rows simply fall through to the existing phash/fingerprint rules.
"""
from __future__ import annotations

import re
from typing import Optional

# (label, pattern). First match wins, so order by specificity.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # AutoScout24: a UUID at the end of the slug on every locale domain,
    # with or without a cat_maNNmoNNNN category token in front of it.
    ("as24", re.compile(
        r"autoscout24\.[a-z.]+/.*?"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)),
    # Facebook Marketplace item id. Trailing slash and ?ref= tracking vary.
    ("fb", re.compile(r"facebook\.com/marketplace/item/(\d+)", re.I)),
    # eBay item number - identical across every national eBay domain.
    ("ebay", re.compile(r"ebay\.[a-z.]+/itm/(?:[^/?]*/)?(\d{9,})", re.I)),
    # Kleinanzeigen advert id, trailing segment of the s-anzeige path.
    ("kleinanzeigen", re.compile(r"kleinanzeigen\.de/s-anzeige/[^/]*/(\d+)", re.I)),
    # Car & Classic: /l/<id> for listings, /la/<id> for auctions.
    ("carandclassic", re.compile(r"carandclassic\.com/(?:l|la)/([A-Za-z0-9]+)", re.I)),
    ("marktplaats", re.compile(r"marktplaats\.nl/[^/]*?/?m(\d{7,})", re.I)),
]


def stable_key(url: Optional[str]) -> Optional[str]:
    """Return a `<site>:<id>` identity for `url`, or None if unrecognised."""
    if not url:
        return None
    for label, pattern in _PATTERNS:
        match = pattern.search(url)
        if match:
            return f"{label}:{match.group(1).lower()}"
    return None
