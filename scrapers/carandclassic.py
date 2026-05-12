from __future__ import annotations

import html as html_module
import json
import re
from typing import List, Optional

from core.http_client import polite_get
from core.models import Listing
from scrapers.base import BaseScraper

BASE_URL = "https://www.carandclassic.com"
# Free-text search; broader "mark 1" wording catches /la/ basics listings
# (e.g. "1970 Ford Escort Mexico Mark 1") that "mk1" misses entirely.
SEARCH_URL = BASE_URL + "/search?q=ford+escort+mark+1&page={page}"

INERTIA_RE = re.compile(
    r'<script\s+data-page="app"\s+type="application/json">(.*?)</script>',
    re.DOTALL,
)

# Accepted URL prefixes — /l/ premium and /la/ basics are active sales listings.
# /car/ is the historical archive (already sold); /auctions/ uses a different
# slug format and a different listing flow; /make-an-offer/ is yet another tier.
_ACCEPT_PREFIXES = ("/l/", "/la/")


class CarAndClassicScraper(BaseScraper):
    site_name = "carandclassic"

    def fetch_listings(self) -> List[Listing]:
        results: List[Listing] = []
        page = 1

        while True:
            url = SEARCH_URL.format(page=page)
            self.log.info("Fetching page %d: %s", page, url)
            try:
                resp = polite_get(
                    self.http,
                    url,
                    min_delay=self.config.get("request_delay_min", 2.0),
                    max_delay=self.config.get("request_delay_max", 5.0),
                )
            except Exception as exc:
                self.log.warning("Request failed on page %d: %s", page, exc)
                break

            m = INERTIA_RE.search(resp.text)
            if not m:
                self.log.warning("No Inertia data-page script found on page %d", page)
                break

            try:
                data = json.loads(html_module.unescape(m.group(1)))
            except json.JSONDecodeError as exc:
                self.log.warning("JSON parse error on page %d: %s", page, exc)
                break

            sr = data.get("props", {}).get("searchResults", {})
            raw_listings = sr.get("data") or []
            if not raw_listings:
                self.log.info("No listings on page %d — stopping", page)
                break

            for item in raw_listings:
                listing = self._parse_item(item)
                if listing:
                    results.append(listing)

            pagination = sr.get("pagination", {})
            last_page = pagination.get("last_page", 1)
            if page >= last_page:
                break
            page += 1

        self.log.info("Car & Classic total listings: %d", len(results))
        return results

    def _parse_item(self, item: dict) -> Optional[Listing]:
        try:
            # URL — accept only /l/ and /la/ (active sales). Skip archive/auctions.
            rel_url = item.get("url") or f"/l/{item.get('slug', '')}"
            if not any(rel_url.startswith(p) for p in _ACCEPT_PREFIXES):
                return None
            url = BASE_URL + rel_url if rel_url.startswith("/") else rel_url

            # Skip listings already marked sold in the JSON
            if item.get("isSold"):
                return None

            title = item.get("title") or ""
            if "escort" not in title.lower():
                return None

            year_raw = item.get("year")
            year = int(year_raw) if year_raw else None

            attrs = item.get("attributes") or {}
            # Preserve whatever steering side is declared (lhd/rhd/None).
            # RHD is no longer rejected — surface it to the digest with the
            # value intact so the buyer sees an accurate "Drive:" badge.
            steering = (attrs.get("steeringPosition") or "").lower() or None

            # Price (minor units in JSON)
            price_obj = item.get("price") or {}
            price_value_minor = price_obj.get("value")
            currency_obj = price_obj.get("currency") or {}
            currency = currency_obj.get("name")
            raw_price = None
            price_val = None
            if price_value_minor is not None:
                price_val = int(price_value_minor)
                symbol = currency_obj.get("symbol", currency or "")
                raw_price = f"{symbol}{price_val / 100:,.0f}"

            images = item.get("images") or []
            image_url = images[0].get("url") if images else None
            # /la/ (basics tier) listings return relative paths like
            # "/uploads/cars/ford/…"; premium /l/ listings return absolute URLs.
            # Normalise so the value is always a fetchable absolute URL.
            if image_url and image_url.startswith("/"):
                image_url = "https://assets.carandclassic.com" + image_url

            loc = item.get("location") or {}
            country = (loc.get("countryCode") or "").upper() or None
            town = loc.get("town") or ""
            location_str = ", ".join(filter(None, [town, country])) or None

            description = _build_synthetic_description(attrs, loc)

            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                location=location_str,
                country_code=country,
                image_url=image_url,
                steering=steering or "unknown",
                description=description,
            )
        except Exception as exc:
            self.log.debug("Failed to parse item: %s — %s", item.get("title"), exc)
            return None


def _build_synthetic_description(attrs: dict, loc: dict) -> Optional[str]:
    """Build a one-line summary from the attributes block.

    The search-results JSON doesn't include the seller's free-text description,
    only structured fields. Composing them gives the digest reader the gist
    (mileage, engine, colour) without an extra detail-page request per listing.
    """
    parts = []
    mileage_str = _format_mileage(attrs.get("mileage"))
    if mileage_str:
        parts.append(mileage_str)
    engine = attrs.get("engineSize")
    if engine:
        parts.append(str(engine))
    fuel = attrs.get("fuelType")
    if fuel:
        parts.append(str(fuel))
    trans = attrs.get("transmissionType")
    if trans:
        parts.append(str(trans))
    colour = attrs.get("colour")
    if colour:
        parts.append(str(colour))
    steering = attrs.get("steeringPosition")
    if steering:
        parts.append(steering.upper())
    return " • ".join(parts) or None


def _format_mileage(mileage) -> Optional[str]:
    """Render Car & Classic's mileage attribute.

    The JSON stores mileage as `{"value": <int>, "unit": "mi"|"km"}` for
    structured listings, but older or sparse rows can carry a bare int. Skip
    placeholder values (0 / 1) — those are "seller didn't say" markers that
    would otherwise read as "0 mi" in the digest.
    """
    if mileage is None:
        return None
    value, unit = None, "mi"
    if isinstance(mileage, dict):
        v = mileage.get("value")
        if isinstance(v, (int, float)):
            value = int(v)
        u = mileage.get("unit")
        if isinstance(u, str) and u:
            unit = u
    elif isinstance(mileage, (int, float)):
        value = int(mileage)
    if value is None or value <= 1:
        return None
    return f"{value:,} {unit}"
