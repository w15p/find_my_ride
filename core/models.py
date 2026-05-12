from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Listing:
    url: str                        # Canonical URL — used as the deduplication key
    site_name: str                  # e.g. "carandclassic", "autoscout24"
    title: str
    price: Optional[str] = None     # Raw display string, e.g. "€12,500" or "POA"
    price_value: Optional[int] = None       # Integer minor units (pence/cents) for sorting
    price_currency: Optional[str] = None    # ISO 4217, e.g. "EUR" / "GBP"
    year: Optional[int] = None
    location: Optional[str] = None          # Human-readable, e.g. "Amersfoort, NL"
    country_code: Optional[str] = None      # ISO 3166-1 alpha-2
    image_url: Optional[str] = None
    steering: Optional[str] = None          # "lhd" | "rhd" | "unknown"
    body_type: Optional[str] = None         # "2-door", "saloon", etc. — best-effort
    description: Optional[str] = None       # Seller-written body text or synthetic summary
    image_phash: Optional[str] = None       # 16-char hex perceptual hash for cross-source dedupe
    fingerprint: Optional[str] = None       # sha1 of normalised dedupe key
    canonical_url: Optional[str] = None     # If duplicate, points at canonical row's URL; NULL = canonical
    sold_signals_count: int = 0             # Two-strike rule: only mark sold once this hits 2
    scraped_at: datetime = field(default_factory=datetime.utcnow)
    status: str = "active"                  # "active" | "sold" | "expired"
    sold_at: Optional[datetime] = None
