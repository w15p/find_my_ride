from __future__ import annotations

import html as html_module
import json
import re
from typing import List, Optional
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from core.http_client import polite_get
from core.models import Listing
from scrapers.base import BaseScraper

BASE_URL = "https://www.carandclassic.com"
# Query string is per-search (`self.query`); URL-encoded into the C&C free-text
# search endpoint. For the default cars hunt, "ford escort mk1" gives broader
# matches via C&C's own fuzzy matching than the prior literal "mark 1" did.
SEARCH_URL_TMPL = BASE_URL + "/search?q={q}&page={page}"

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
            url = SEARCH_URL_TMPL.format(q=quote_plus(self.query), page=page)
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

        # Enrich each result with the seller's free-text description from
        # the detail page. Cheap — typical scrape has 3-15 candidate listings.
        # The synthetic spec line ("73,650 km • 1100 • petrol...") stays as
        # the leading summary; seller's prose appends underneath.
        for listing in results:
            detail = self._fetch_description(listing.url, title=listing.title)
            if detail:
                if listing.description:
                    combined = f"{listing.description}\n\n{detail}"
                    listing.description = combined[:1000]
                else:
                    listing.description = detail[:1000]

        self.log.info("Car & Classic total listings: %d", len(results))
        return results

    def _fetch_description(self, url: str, title: Optional[str] = None) -> Optional[str]:
        """Return the seller's free-text description from a C&C detail page.

        Premium (/l/) and basics (/la/) listings both embed it at
        `props.listing.description` in the Inertia JSON. Basics ships HTML
        (`<br/>` and friends) and additionally masks the model name with
        asterisks (e.g. "Ford ****** MK1") — anti-scrape on the free tier.
        We strip HTML and substitute the mask with the model word extracted
        from the title (every word that's not Ford, year, Mark, etc.).
        """
        try:
            resp = polite_get(
                self.http, url,
                min_delay=self.config.get("request_delay_min", 1.0),
                max_delay=self.config.get("request_delay_max", 2.0),
            )
        except Exception as exc:
            self.log.debug("C&C detail fetch failed: %s — %s", url, exc)
            return None
        m = INERTIA_RE.search(resp.text)
        if not m:
            return None
        try:
            data = json.loads(html_module.unescape(m.group(1)))
        except json.JSONDecodeError:
            return None
        raw = (data.get("props") or {}).get("listing", {}).get("description")
        if not raw:
            return None
        text = BeautifulSoup(raw, "lxml").get_text(" ", strip=True) if "<" in raw else raw
        text = re.sub(r"\s+", " ", text).strip()
        # Unmask the model name. Basics tier replaces it with 3+ asterisks.
        if "***" in text and title:
            model = _model_word_from_title(title)
            if model:
                text = re.sub(r"\*{3,}", model, text)
        return text[:1000] or None

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
            if not self.title_matches_search(title):
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


_TITLE_STOPWORDS = {
    "ford", "mark", "mk", "mk1", "mki", "mk2", "mk3", "mk4", "mk5",
    "rs", "gt", "lhd", "rhd", "the", "a", "an", "of",
    "mexico", "twin", "cam", "cosworth", "rs1600", "rs2000",
    "spec", "for", "sale",
}


def _model_word_from_title(title: str) -> Optional[str]:
    """Pull the distinctive model word from a C&C listing title.

    Used to unmask the `******` placeholder C&C inserts into basics-tier
    descriptions. The title is plain — e.g. `1970 Ford Escort Mexico Mark 1`.
    Strip year, make, trim/variant words; keep the first remaining word
    (the model name). Casing preserved so we don't slap `escort` into prose
    that capitalised it.
    """
    if not title:
        return None
    for raw in re.findall(r"[A-Za-z]+", title):
        if raw.lower() in _TITLE_STOPWORDS:
            continue
        return raw
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
