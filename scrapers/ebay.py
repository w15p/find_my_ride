from __future__ import annotations

import os
import re
import time
from typing import List, Optional

from core.models import Listing
from scrapers.base import BaseScraper

OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# eBay classic/vintage car category
CLASSIC_CAR_CATEGORY = "9801"

LHD_KEYWORDS = {"lhd", "left hand drive", "left-hand drive", "linksteuerung"}


class EbayScraper(BaseScraper):
    site_name = "ebay"

    def __init__(self, config: dict, http_client) -> None:
        super().__init__(config, http_client)
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

        app_id = os.environ.get("EBAY_APP_ID") or self.config.get("app_id", "")
        cert_id = os.environ.get("EBAY_CERT_ID") or self.config.get("cert_id", "")
        if not app_id or not cert_id:
            raise RuntimeError(
                "eBay credentials missing. Set EBAY_APP_ID and EBAY_CERT_ID env vars."
            )

        resp = self.http.post(
            OAUTH_URL,
            data={"grant_type": "client_credentials", "scope": SCOPE},
            auth=(app_id, cert_id),
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + int(data.get("expires_in", 7200))
        return self._token

    def fetch_listings(self) -> List[Listing]:
        marketplaces = self.config.get("marketplaces", ["EBAY_GB", "EBAY_DE", "EBAY_NL"])
        category_id = self.config.get("category_id", CLASSIC_CAR_CATEGORY)
        results: List[Listing] = []

        try:
            token = self._get_token()
        except RuntimeError as exc:
            self.log.error("eBay auth failed: %s", exc)
            return []

        for marketplace in marketplaces:
            self.log.info("Searching eBay marketplace: %s", marketplace)
            listings = self._search_marketplace(token, marketplace, category_id)
            results.extend(listings)

        self.log.info("Total eBay listings found: %d", len(results))
        return results

    def _search_marketplace(
        self, token: str, marketplace: str, category_id: str
    ) -> List[Listing]:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "Content-Type": "application/json",
        }
        params = {
            "q": "ford escort mk1 lhd",
            "category_ids": category_id,
            "sort": "newlyListed",
            "limit": "200",
        }
        listings: List[Listing] = []
        offset = 0

        while True:
            params["offset"] = str(offset)
            try:
                resp = self.http.get(SEARCH_URL, headers=headers, params=params, timeout=20)
                if resp.status_code == 429:
                    self.log.warning("eBay rate limit hit on %s", marketplace)
                    break
                resp.raise_for_status()
            except Exception as exc:
                self.log.warning("eBay request failed for %s: %s", marketplace, exc)
                break

            data = resp.json()
            items = data.get("itemSummaries") or []
            for item in items:
                listing = self._parse_item(item, marketplace)
                if listing:
                    listings.append(listing)

            total = data.get("total", 0)
            offset += len(items)
            if offset >= total or not items:
                break

        return listings

    def _parse_item(self, item: dict, marketplace: str) -> Optional[Listing]:
        try:
            title = item.get("title", "")

            # Post-filter: must mention escort. LHD-in-title is no longer
            # required — RHD listings are surfaced (with a "Drive: ?" badge so
            # the buyer sees the steering side). Central filter and reject
            # keywords still cull parts/wrong-variant/out-of-range listings.
            title_lower = title.lower()
            if "escort" not in title_lower:
                return None

            # Steering — populate "lhd" only when the title says so explicitly.
            steering = "lhd" if any(kw in title_lower for kw in LHD_KEYWORDS) else "unknown"

            # Year: look in title
            year = _extract_year(title)
            if year and not (1968 <= year <= 1975):
                return None

            url = item.get("itemWebUrl", "")
            if not url:
                return None

            price_obj = item.get("price") or {}
            raw_price = None
            price_val = None
            currency = price_obj.get("currency")
            price_str = price_obj.get("value")
            if price_str:
                raw_price = f"{currency or ''} {price_str}".strip()
                try:
                    # Store in minor units (pence/cents)
                    price_val = int(float(price_str) * 100)
                except (ValueError, TypeError):
                    pass

            loc = item.get("itemLocation") or {}
            country = loc.get("country", "")
            city = loc.get("city", "")
            location_str = ", ".join(filter(None, [city, country])) or None

            image = item.get("image") or {}
            image_url = image.get("imageUrl")

            description_raw = item.get("shortDescription") or ""
            description = re.sub(r"\s+", " ", description_raw).strip()[:1000] or None

            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                location=location_str,
                country_code=country or None,
                image_url=image_url,
                steering=steering,
                description=description,
            )
        except Exception as exc:
            self.log.debug("Failed to parse eBay item: %s", exc)
            return None


def _extract_year(text: str) -> Optional[int]:
    match = re.search(r"\b(196[89]|197[0-5])\b", text)
    if match:
        return int(match.group(1))
    return None
