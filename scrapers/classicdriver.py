from __future__ import annotations

import re
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.http_client import polite_get
from core.models import Listing
from scrapers.base import BaseScraper

BASE_URL = "https://www.classicdriver.com"
SEARCH_URL = (
    BASE_URL
    + "/en/cars/ford/escort"
    "?filter[model_year_from]=1968"
    "&filter[model_year_to]=1975"
    "&filter[steering]=left"
    "&sort=newest"
)


class ClassicDriverScraper(BaseScraper):
    site_name = "classicdriver"

    def fetch_listings(self) -> List[Listing]:
        results: List[Listing] = []
        url = SEARCH_URL
        page = 1
        max_pages = self.config.get("max_pages", 5)

        while page <= max_pages:
            self.log.info("Fetching page %d: %s", page, url)
            try:
                resp = polite_get(self.http, url)
            except Exception as exc:
                self.log.warning("Request failed on page %d: %s", page, exc)
                break

            soup = BeautifulSoup(resp.text, "lxml")

            # Each listing card is a div.related-item-wishlist
            cards = soup.select("div.related-item-wishlist")
            if not cards:
                self.log.info("No cards found on page %d — stopping", page)
                break

            for card in cards:
                listing = self._parse_card(card)
                if listing:
                    results.append(listing)

            next_link = soup.select_one("a[rel='next'], a.pager-next, li.next a")
            if not next_link or not next_link.get("href"):
                break
            url = urljoin(BASE_URL, next_link["href"])
            page += 1

        self.log.info("Classic Driver total listings: %d", len(results))
        return results

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link_el = card.select_one("a[href*='/en/car/']")
            if not link_el:
                return None
            href = link_el.get("href", "")
            url = urljoin(BASE_URL, href)

            title_el = card.select_one(".related-item-title a")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                title = link_el.get_text(strip=True)
            if not self.title_matches_search(title):
                return None

            # Extract year from title (any 4-digit year)
            year = _extract_any_year(title)
            if year and not (1968 <= year <= 1975):
                return None  # discard out-of-range (Mk2, Cosworth, etc.)

            # Price: inside .related-item-price-location-flag > .price
            price_el = card.select_one(".price")
            raw_price = price_el.get_text(strip=True) if price_el else None
            price_val, currency = _parse_price_string(raw_price)

            # Image: data-srcset="url 431w" — must parse the URL from the srcset string
            img_el = card.select_one("img")
            image_url = None
            if img_el:
                srcset = img_el.get("data-srcset") or img_el.get("srcset") or ""
                if srcset:
                    # srcset format: "https://... 431w" — take everything before the space
                    image_url = srcset.split(" ")[0]
                if not image_url:
                    src = img_el.get("data-src") or img_el.get("src") or ""
                    # Skip base64 SVG placeholders
                    if src and not src.startswith("data:"):
                        image_url = src

            # Location: flag img alt text gives country name
            flag_el = card.select_one(".related-item-price-location-flag img")
            location = flag_el.get("title") or flag_el.get("alt") if flag_el else None

            # Synthetic description — list cards don't carry a body. Pull any
            # visible secondary text (dealer name, subtitle, mileage badge) into
            # a one-line summary so the digest has *something* to show.
            subtitle_el = card.select_one(".related-item-subtitle, .dealer-name, .related-item-dealer")
            subtitle = subtitle_el.get_text(strip=True) if subtitle_el else None
            parts = [p for p in (str(year) if year else None, location, subtitle) if p]
            description = " • ".join(parts) or None

            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                location=location,
                image_url=image_url,
                steering="lhd",  # filtered by URL param
                description=description,
            )
        except Exception as exc:
            self.log.debug("Failed to parse Classic Driver card: %s", exc)
            return None


def _extract_any_year(text: str) -> Optional[int]:
    """Extract the first plausible car year (1950–2010) from text."""
    match = re.search(r"\b(19[5-9]\d|200\d|201[0-9])\b", text)
    return int(match.group(1)) if match else None


def _parse_price_string(raw: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    if not raw:
        return None, None
    currency = None
    if "€" in raw or "EUR" in raw.upper():
        currency = "EUR"
    elif "£" in raw or "GBP" in raw.upper():
        currency = "GBP"
    elif "$" in raw or "USD" in raw.upper():
        currency = "USD"
    digits = re.sub(r"[^\d]", "", raw)
    if digits:
        # Classic Driver shows full price like "USD 218 797" — store in minor units
        return int(digits) * 100, currency
    return None, currency
