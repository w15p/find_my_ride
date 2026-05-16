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

    def _enrich_from_detail(self, listing: Listing, token: str, marketplace: str) -> None:
        """Pull structured aspects from the eBay item-detail endpoint.

        The search endpoint omits the `localizedAspects` block, but the detail
        endpoint returns Year, Drive Side, Body Type, Mileage, etc. as clean
        strings. We call it once per candidate that survived the search-side
        filter so a missing year-in-title doesn't strand the row at year=None.
        Year and Drive Side overwrite scraped values; description is filled
        only when our previous extraction was empty.
        """
        m = re.search(r"/itm/(\d+)", listing.url) or re.search(r"item/v1\|(\d+)", listing.url)
        if not m:
            return
        item_id = f"v1|{m.group(1)}|0"
        try:
            resp = self.http.get(
                f"https://api.ebay.com/buy/browse/v1/item/{item_id}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": marketplace,
                },
                timeout=15,
            )
            if resp.status_code != 200:
                return
            data = resp.json()
        except Exception as exc:
            self.log.debug("eBay enrich failed for %s: %s", listing.url, exc)
            return
        aspects = {a.get("name"): a.get("value") for a in (data.get("localizedAspects") or [])}
        if listing.year is None:
            try:
                listing.year = int(aspects.get("Year") or 0) or None
            except (ValueError, TypeError):
                pass
        drive = (aspects.get("Drive Side") or "").lower()
        if "right" in drive:
            listing.steering = "rhd"
        elif "left" in drive:
            listing.steering = "lhd"
        if not listing.description:
            short = data.get("shortDescription") or ""
            short = " ".join(short.split())
            if short:
                listing.description = short[:1000]

    def _search_marketplace(
        self, token: str, marketplace: str, category_id: str
    ) -> List[Listing]:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": marketplace,
            "Content-Type": "application/json",
        }
        # No `lhd` in the query — UK sellers don't add it (RHD is the default
        # there), and DE/NL sellers don't either (LHD is the default for them).
        # No `category_ids` — eBay's category tree differs per marketplace
        # (UK 9801 ≠ DE 9805 etc.), so a single ID throws away most of what
        # we want. Volume gets large on DE/NL (~10k+ each, mostly parts) but
        # the central reject-keywords + $2K USD floor in `_should_keep` cull
        # everything that isn't a real car.
        params = {
            "q": "ford escort mk1",
            "sort": "newlyListed",
            "limit": "200",
        }
        max_pages = int(self.config.get("max_pages", 5))
        listings: List[Listing] = []
        offset = 0
        page = 0

        while page < max_pages:
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
                    self._enrich_from_detail(listing, token, marketplace)
                    listings.append(listing)

            total = data.get("total", 0)
            offset += len(items)
            page += 1
            if offset >= total or not items:
                break
        else:
            self.log.info("eBay %s: hit max_pages=%d cap at offset=%d/%d", marketplace, max_pages, offset, total)

        return listings

    def _parse_item(self, item: dict, marketplace: str) -> Optional[Listing]:
        try:
            title = item.get("title", "")

            # Post-filter: must mention escort. LHD-in-title is no longer
            # required — RHD listings are surfaced (with a "Drive: ?" badge so
            # the buyer sees the steering side). Central filter and reject
            # keywords still cull parts/wrong-variant/out-of-range listings.
            title_lower = title.lower()
            if not self.title_matches_search(title):
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

            # eBay names these backwards: `image.imageUrl` is a 225px thumbnail,
            # `thumbnailImages[0].imageUrl` is the 1600px hero. Prefer the hero;
            # fall back to the thumbnail; as last resort, derive from the
            # listing URL's `:g:<hash>` suffix which encodes the same image ID.
            thumbs = item.get("thumbnailImages") or []
            image_url = (thumbs[0].get("imageUrl") if thumbs else None) \
                        or (item.get("image") or {}).get("imageUrl")
            if not image_url:
                m = re.search(r":g:([A-Za-z0-9]+)", url or "")
                if m:
                    image_url = f"https://i.ebayimg.com/images/g/{m.group(1)}/s-l1600.jpg"

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
