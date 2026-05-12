from __future__ import annotations

import json
import os
import re
import time
from typing import List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from core.http_client import polite_get
from core.models import Listing
from scrapers.base import BaseScraper

API_TOKEN_URL = "https://auth.marktplaats.nl/oauth/token"
API_SEARCH_URL = "https://api.marktplaats.nl/v1/search"
HTML_SEARCH_URL = "https://www.marktplaats.nl/q/ford+escort+mk1/"
BASE_URL = "https://www.marktplaats.nl"

LHD_KEYWORDS = {"lhd", "left hand drive", "left-hand drive", "linksgestuurd", "links gestuurd"}


class MarktplaatsScraper(BaseScraper):
    site_name = "marktplaats"

    def __init__(self, config: dict, http_client) -> None:
        super().__init__(config, http_client)
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

    def fetch_listings(self) -> List[Listing]:
        """Try the official API first; fall back to HTML scraping."""
        client_id = os.environ.get("MARKTPLAATS_CLIENT_ID") or self.config.get("client_id")
        client_secret = os.environ.get("MARKTPLAATS_CLIENT_SECRET") or self.config.get("client_secret")

        if client_id and client_secret:
            try:
                return self._fetch_via_api(client_id, client_secret)
            except Exception as exc:
                self.log.warning("Marktplaats API failed (%s), falling back to HTML", exc)

        return self._fetch_via_html()

    # ------------------------------------------------------------------ API path

    def _get_token(self, client_id: str, client_secret: str) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        resp = self.http.post(
            API_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _fetch_via_api(self, client_id: str, client_secret: str) -> List[Listing]:
        token = self._get_token(client_id, client_secret)
        headers = {"Authorization": f"Bearer {token}"}
        params = {
            "query": "ford escort mk1 lhd",
            "categoryId": self.config.get("category_id", 91),
            "limit": 100,
            "sortBy": "default",
            "sortOrder": "descending",
        }
        resp = self.http.get(API_SEARCH_URL, headers=headers, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        ads = data.get("advertisements") or data.get("listings") or []
        results = []
        for ad in ads:
            listing = self._parse_api_ad(ad)
            if listing:
                results.append(listing)
        self.log.info("Marktplaats API: %d listings", len(results))
        return results

    def _parse_api_ad(self, ad: dict) -> Optional[Listing]:
        try:
            title = ad.get("title", "")
            if "escort" not in title.lower():
                return None
            title_lower = title.lower()
            # LHD-in-title no longer required — RHD listings allowed through.
            steering = "lhd" if any(kw in title_lower for kw in LHD_KEYWORDS) else "unknown"

            url = ad.get("url") or ad.get("vipUrl") or ""
            if not url.startswith("http"):
                url = urljoin(BASE_URL, url)

            price_info = ad.get("priceInfo") or {}
            raw_price = price_info.get("priceCents")
            currency = "EUR"
            price_val = None
            if raw_price is not None:
                price_val = int(raw_price)
                raw_price = f"€{price_val / 100:.0f}"

            images = ad.get("images") or []
            image_url = images[0].get("medium") or images[0].get("url") if images else None

            loc = ad.get("location") or {}
            city = loc.get("cityName") or ""
            country = "NL"
            location_str = ", ".join(filter(None, [city, country]))

            year = _extract_year(title)

            description_raw = ad.get("description") or ""
            description = re.sub(r"\s+", " ", description_raw).strip()[:1000] or None

            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                location=location_str or None,
                country_code=country,
                image_url=image_url,
                steering=steering,
                description=description,
            )
        except Exception as exc:
            self.log.debug("Failed to parse Marktplaats API ad: %s", exc)
            return None

    # ------------------------------------------------------------------ HTML fallback

    def _fetch_via_html(self) -> List[Listing]:
        self.log.info("Using Marktplaats HTML fallback")
        results: List[Listing] = []
        try:
            resp = polite_get(self.http, HTML_SEARCH_URL)
        except Exception as exc:
            self.log.warning("Marktplaats HTML request failed: %s", exc)
            return []

        soup = BeautifulSoup(resp.text, "lxml")

        # Try JSON-LD structured data first (most reliable)
        for script in soup.select("script[type='application/ld+json']"):
            try:
                data = json.loads(script.string or "")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    listing = self._parse_jsonld(item)
                    if listing:
                        results.append(listing)
            except (json.JSONDecodeError, TypeError):
                continue

        if results:
            self.log.info("Marktplaats HTML (JSON-LD): %d listings", len(results))
            return results

        # Fallback: parse listing cards
        cards = soup.select("article[data-item-id], li[data-item-id], .listing-item")
        for card in cards:
            listing = self._parse_html_card(card)
            if listing:
                results.append(listing)

        self.log.info("Marktplaats HTML (cards): %d listings", len(results))
        return results

    def _parse_jsonld(self, data: dict) -> Optional[Listing]:
        try:
            if data.get("@type") not in ("Product", "Offer", "ItemPage", "Vehicle"):
                return None
            name = data.get("name") or data.get("title") or ""
            if "escort" not in name.lower():
                return None
            steering = "lhd" if any(kw in name.lower() for kw in LHD_KEYWORDS) else "unknown"
            url = data.get("url") or data.get("@id") or ""
            if not url.startswith("http"):
                url = urljoin(BASE_URL, url)
            offers = data.get("offers") or {}
            raw_price = offers.get("price")
            currency = offers.get("priceCurrency", "EUR")
            price_val = int(float(raw_price) * 100) if raw_price else None
            image = data.get("image")
            image_url = image[0] if isinstance(image, list) else image
            description_raw = data.get("description") or ""
            description = re.sub(r"\s+", " ", description_raw).strip()[:1000] or None
            return Listing(
                url=url,
                site_name=self.site_name,
                title=name,
                price=f"€{raw_price}" if raw_price else None,
                price_value=price_val,
                price_currency=currency,
                year=_extract_year(name),
                country_code="NL",
                image_url=image_url,
                steering=steering,
                description=description,
            )
        except Exception as exc:
            self.log.debug("JSON-LD parse error: %s", exc)
            return None

    def _parse_html_card(self, card) -> Optional[Listing]:
        try:
            link = card.select_one("a[href]")
            if not link:
                return None
            href = link.get("href", "")
            url = urljoin(BASE_URL, href)
            title_el = card.select_one("h2, h3, .title, [class*='title']")
            title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
            if "escort" not in title.lower():
                return None
            steering = "lhd" if any(kw in title.lower() for kw in LHD_KEYWORDS) else "unknown"
            price_el = card.select_one("[class*='price']")
            raw_price = price_el.get_text(strip=True) if price_el else None
            img = card.select_one("img")
            image_url = img.get("src") or img.get("data-src") if img else None
            desc_el = card.select_one("[class*='description'], p")
            description = desc_el.get_text(strip=True)[:1000] if desc_el else None
            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                year=_extract_year(title),
                country_code="NL",
                image_url=image_url,
                steering=steering,
                description=description,
            )
        except Exception as exc:
            self.log.debug("Marktplaats card parse error: %s", exc)
            return None


def _extract_year(text: str) -> Optional[int]:
    match = re.search(r"\b(196[89]|197[0-5])\b", text)
    return int(match.group(1)) if match else None
