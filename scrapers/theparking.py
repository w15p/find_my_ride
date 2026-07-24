from __future__ import annotations

import html as html_module
import re
from typing import List, Optional

from core.http_client import polite_get
from core.models import Listing
from scrapers.base import BaseScraper

BASE_URL = "https://www.theparking.eu"
# theparking.eu is a classifieds AGGREGATOR - it re-lists cars from many EU
# source sites (gocar.be, 2ememain, mobile.de, autoscout24, carandclassic...).
# Value here is the regional sources our dedicated scrapers DON'T cover; the
# overlap with autoscout24 / carandclassic is handled by the phash+fingerprint
# dedupe in run.py. Plain HTTP, server-rendered - no Playwright needed.
#
# The model path is per-search (site_overrides.theparking.path) because
# theparking's slugs don't derive cleanly from make/model - "ford-escort-mk1",
# "giulia-gt", etc. tri=prix_croissant sorts price-ascending so page 1 is the
# affordable end (which is what survives the per-search price cap anyway).
SEARCH_URL_TMPL = BASE_URL + "/used-cars/{path}.html?tri=prix_croissant"

# Each listing card starts with the image/source anchor:
#   <a rel="nofollow" class="external" name="SOURCE_DOMAIN" ... href="/tools/ID/...">
# We split the page on that marker; everything up to the next one is one card.
_CARD_SPLIT = re.compile(r'<a\s+rel="nofollow"\s+class="external"\s+name="')

_YEAR_RE = re.compile(r"\b(19[3-9]\d|20[0-2]\d)\b")


class TheParkingScraper(BaseScraper):
    site_name = "theparking"

    def fetch_listings(self) -> List[Listing]:
        path = (self.extra_params or {}).get("path")
        if not path:
            self.log.warning(
                "theparking: no `path` override configured for this search - "
                "skipping (needs site_overrides.theparking.path, e.g. "
                "'ford-escort-mk1')."
            )
            return []

        url = SEARCH_URL_TMPL.format(path=path)
        self.log.info("Fetching theparking: %s", url)
        try:
            resp = polite_get(self.http, url)
        except Exception as exc:
            self.log.warning("theparking request failed: %s", exc)
            return []

        cards = _CARD_SPLIT.split(resp.text)
        results: List[Listing] = []
        for seg in cards[1:]:
            listing = self._parse_card(seg)
            if listing:
                results.append(listing)
        self.log.info("theparking total listings: %d", len(results))
        return results

    def _parse_card(self, seg: str) -> Optional[Listing]:
        try:
            seg = seg[:4000]  # a card is well under this; bound the regex work
            # Source domain is the text right after the split marker.
            src = re.match(r'([^"]+)"', seg)
            source = src.group(1) if src else None

            # theparking numeric id (in the image filename + fav classes).
            idm = re.search(r"_(\d{6,})\.jpg", seg) or re.search(r'data-id="(\d{6,})"', seg)
            tp_id = idm.group(1) if idm else None

            # Detail-page path (stable per-listing URL on theparking).
            slug = re.search(r'(/used-cars-detail/[^"]+?/[A-Z0-9]{6,}\.html)', seg)
            if not slug or not tp_id:
                return None
            url = BASE_URL + slug.group(1)

            # Title: the image alt carries the fullest descriptor; fall back to
            # the title-block spans (brand + model + variant).
            alt = re.search(r'alt="([^"]+)"', seg)
            title = html_module.unescape(alt.group(1).strip()) if alt else ""
            if not title:
                block = seg[:2500].split('class="external tag_f_titre"')
                if len(block) > 1:
                    spans = re.findall(r"<span[^>]*>(.*?)</span>", block[1][:400], re.S)
                    title = " ".join(
                        re.sub(r"<[^>]+>", "", s).strip() for s in spans if s.strip()
                    )
            title = re.sub(r"\s+", " ", title).strip()
            title = title.title() if title.isupper() else title
            if not title:
                return None
            if not self.title_matches_search(title):
                return None

            # Price: <p class="prix"> 42 500 € </p> (or POA).
            prm = re.search(r'class="prix">\s*([^<]+?)\s*</p>', seg)
            raw_price = html_module.unescape(prm.group(1).strip()) if prm else None
            price_val = None
            currency = None
            if raw_price:
                digits = re.sub(r"[^\d]", "", raw_price)
                if digits:
                    price_val = int(digits) * 100  # EUR major -> minor units
                    currency = "EUR"
                    raw_price = f"€{int(digits):,}"

            # Year: parse from the title/slug. Prefer a classic-plausible year.
            year = None
            for m in _YEAR_RE.findall(title + " " + slug.group(1).replace("-", " ")):
                y = int(m)
                if 1950 <= y <= 1990:
                    year = y
                    break

            # Image on leparking.fr CDN.
            img = re.search(r'src="(https://img\.leparking\.fr/[^"]+)"', seg)
            image_url = img.group(1) if img else None

            # Source site surfaced in the description so the review UI shows
            # where the aggregator pulled it from. No location/country in the
            # card - left None (the country allowlist treats unknown as
            # not-blocked; theparking's sources are EU).
            description = f"via theparking (source: {source})" if source else "via theparking"

            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                image_url=image_url,
                steering="unknown",
                description=description,
            )
        except Exception as exc:
            self.log.debug("theparking card parse error: %s", exc)
            return None
