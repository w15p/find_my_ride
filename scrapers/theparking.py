from __future__ import annotations

import html as html_module
import json
import random
import re
import time
from typing import List, Optional, Tuple

from core.http_client import USER_AGENTS
from core.models import Listing, MAX_DESCRIPTION_CHARS
from scrapers.base import BaseScraper

BASE_URL = "https://www.theparking.eu"
# theparking.eu is a classifieds AGGREGATOR - it re-lists cars from many EU
# source sites (gocar.be, subito.it, leboncoin.fr, kleinanzeigen.de,
# pistonheads, olx.pt...). Its value is the regional sources our dedicated
# scrapers don't cover.
#
# theparking is a STARTING POINT, not the target: its own pages carry no
# seller text (the detail page's meta description is just "FORD ESCORT") and
# only thumbnail-grade images on the results page. So for each card we walk
# through to the real listing:
#
#   results page  ->  /used-cars-detail/.../<ID>.html   (full-size image +
#                                                        the outbound link)
#   detail page   ->  /tools/<ID>/0/<x>.html            (302, needs Referer -
#                                                        403 without one)
#   302 Location  ->  the real source listing URL       (stored as the
#                                                        listing url, so the
#                                                        card links straight
#                                                        to the seller and
#                                                        dedupes against the
#                                                        same URL from other
#                                                        scrapers)
#   source page   ->  og:description + og:image         (real seller text, so
#                                                        the translation
#                                                        pipeline has input)
#
# Roughly 60% of source sites serve us the description; the rest (gocar.be,
# subito.it, car.gr) 403 a non-browser request. Every hop degrades
# gracefully - a blocked source still yields a listing with the theparking
# image and the real source URL.
SEARCH_URL_TMPL = BASE_URL + "/used-cars/{path}.html?tri=prix_croissant"

# Each result card starts with the image anchor, whose name= attribute is the
# source site's domain. Splitting on it keeps a card's title/price/detail-link
# together (the detail page's own og:title is a social-share string and it has
# no price element, so the card is the reliable place to read both).
_CARD_SPLIT = re.compile(r'<a\s+rel="nofollow"\s+class="external"\s+name="')
_DETAIL_RE = re.compile(r'(/used-cars-detail/[^"]+?/[A-Z0-9]{6,}\.html)')
_TOOLS_RE = re.compile(r'href="(/tools/[A-Z0-9]+/0/[A-Za-z]\.html)"')
_CLOUD_IMG_RE = re.compile(r'(https://cloud\.leparking\.fr/[^"\'\s]+\.jpg)')
_YEAR_RE = re.compile(r"\b(19[3-9]\d|20[0-2]\d)\b")

_OG_DESC_RE = re.compile(
    r'<meta[^>]*property=["\']og:description["\'][^>]*content=["\']([^"\']{25,})["\']', re.I)
_META_DESC_RE = re.compile(
    r'<meta[^>]*name=["\']description["\'][^>]*content=["\']([^"\']{25,})["\']', re.I)
_OG_IMG_RE = re.compile(
    r'<meta[^>]*property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']', re.I)

# Source-domain TLD -> ISO country. theparking carries no location field, but
# its sources are national classifieds, so the TLD is a reliable origin
# signal. Worth setting because country_code is the language prior for
# detect_and_translate (langdetect alone mis-called Italian as Portuguese on
# short, upper-case car text) and it feeds the per-search country allowlist.
# Generic TLDs (.com/.eu/.net) yield None - unknown, which is never blocked.
_TLD_COUNTRY = {
    "de": "DE", "fr": "FR", "it": "IT", "es": "ES", "pt": "PT", "nl": "NL",
    "be": "BE", "at": "AT", "ch": "CH", "dk": "DK", "se": "SE", "no": "NO",
    "fi": "FI", "pl": "PL", "cz": "CZ", "sk": "SK", "hu": "HU", "ro": "RO",
    "bg": "BG", "gr": "GR", "hr": "HR", "si": "SI", "ie": "IE", "lu": "LU",
    "lt": "LT", "lv": "LV", "ee": "EE", "is": "IS", "al": "AL", "uk": "GB",
}


def _country_from_domain(domain: Optional[str]) -> Optional[str]:
    """ISO country from a source domain's TLD ('www.subito.it' -> 'IT')."""
    if not domain:
        return None
    parts = domain.lower().rstrip("/").split(".")
    if len(parts) < 2:
        return None
    tld = parts[-1]
    # co.uk / com.pt style second-level domains
    if tld in ("uk", "pt", "br") and len(parts) >= 3 and parts[-2] in ("co", "com"):
        return _TLD_COUNTRY.get(tld)
    return _TLD_COUNTRY.get(tld)


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

        list_url = SEARCH_URL_TMPL.format(path=path)
        self.log.info("Fetching theparking: %s", list_url)
        body = self._get(list_url)
        if not body:
            return []

        cards = self._parse_cards(body)
        # `max_detail` bounds the per-run cost: each listing costs up to 3
        # requests (detail + redirect + source). Page 1 is ~27 cards.
        cap = int((self.extra_params or {}).get("max_detail", 30))
        cards = [c for c in cards if self.title_matches_search(c["title"])][:cap]
        self.log.info("theparking: %d cards matched, walking through to source", len(cards))

        results: List[Listing] = []
        for i, card in enumerate(cards, 1):
            listing = self._build_listing(card, list_url)
            if listing:
                results.append(listing)
            if i < len(cards):
                time.sleep(random.uniform(0.6, 1.4))

        self.log.info("theparking total listings: %d", len(results))
        return results

    def _parse_cards(self, body: str) -> List[dict]:
        """Title, price, detail-link and source domain per result card."""
        out: List[dict] = []
        for seg in _CARD_SPLIT.split(body)[1:]:
            seg = seg[:4000]
            det = _DETAIL_RE.search(seg)
            alt = re.search(r'alt="([^"]+)"', seg)
            if not det or not alt:
                continue
            title = re.sub(r"\s+", " ", html_module.unescape(alt.group(1))).strip()
            if title.isupper():
                title = title.title()
            dom = re.match(r'([^"]+)"', seg)
            prix = re.search(r'class="prix">\s*([^<]+?)\s*</p>', seg)
            out.append({
                "title": title,
                "price_raw": (html_module.unescape(prix.group(1)).replace("\xa0", " ").strip()
                              if prix else None),
                "detail_url": BASE_URL + det.group(1),
                "source_domain": dom.group(1) if dom else None,
            })
        return out

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _get(self, url: str, referer: Optional[str] = None,
             allow_redirects: bool = True, timeout: int = 20):
        """GET returning response text, or None. Never raises."""
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
            "Upgrade-Insecure-Requests": "1",
        }
        if referer:
            headers["Referer"] = referer
        try:
            resp = self.http.get(url, headers=headers, timeout=timeout,
                                 allow_redirects=allow_redirects)
            if resp.status_code != 200:
                self.log.debug("theparking GET %s -> %s", url[:80], resp.status_code)
                return None
            return resp.text
        except Exception as exc:
            self.log.debug("theparking GET failed %s: %s", url[:80], exc)
            return None

    def _resolve_source_url(self, detail_body: str, detail_url: str) -> Optional[str]:
        """Follow theparking's /tools/ redirect to the real listing URL.

        The redirect 403s without a Referer, so we send the detail page as
        one. Returns the Location target, or None.
        """
        m = _TOOLS_RE.search(detail_body)
        if not m:
            return None
        try:
            resp = self.http.get(
                BASE_URL + m.group(1),
                headers={"User-Agent": random.choice(USER_AGENTS),
                         "Referer": detail_url,
                         "Accept-Language": "en-GB,en;q=0.9"},
                allow_redirects=False, timeout=15,
            )
            loc = resp.headers.get("Location")
            if loc and loc.startswith("http"):
                # Strip theparking's attribution params - keeps the stored URL
                # canonical so it dedupes against the same listing scraped
                # directly by another site scraper.
                return re.sub(r"[?&]utm_[^=]+=[^&]*", "", loc).rstrip("?&")
        except Exception as exc:
            self.log.debug("theparking redirect resolve failed: %s", exc)
        return None

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    @classmethod
    def _jsonld_descriptions(cls, body: str) -> List[str]:
        """Every description string in the page's JSON-LD blocks."""
        found: List[str] = []
        for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                             body, re.S | re.I):
            try:
                data = json.loads(m.group(1).strip())
            except Exception:
                continue

            def walk(node):
                if isinstance(node, dict):
                    d = node.get("description")
                    if isinstance(d, str) and len(d.strip()) > 40:
                        found.append(d.strip())
                    for v in node.values():
                        walk(v)
                elif isinstance(node, list):
                    for v in node:
                        walk(v)

            walk(data)
        return found

    @staticmethod
    def _microdata_descriptions(body: str) -> List[str]:
        """Descriptions from schema.org microdata (`itemprop="description"`).

        Needed for sites that ship no JSON-LD and truncate their og tag -
        motorsportmarkt.de cuts og:description to 129 chars with an ellipsis
        while its microdata holds the full 580. Parsed with BeautifulSoup
        rather than a regex: the container has nested tags, and a non-greedy
        regex stops at the first inner </div> (191 of 580 chars here).
        """
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(body, "html.parser")
            out = []
            for el in soup.select('[itemprop="description"]'):
                t = el.get_text(" ", strip=True)
                if len(t) > 40:
                    out.append(re.sub(r"\s+", " ", t).strip())
            return out
        except Exception:
            return []

    @classmethod
    def _full_description(cls, body: str) -> Optional[str]:
        """The target listing's full seller text.

        Sites truncate og:description (kleinanzeigen ships ~150 chars ending
        in "..." plus a site suffix) while their JSON-LD carries the full
        text - but a listing page also embeds JSON-LD for sidebar "similar
        ads", so simply taking the longest description returns some other
        car entirely (two different Escort pages both yielded the same BMW
        2002 blurb). og:description is truncated but is reliably THIS
        listing's opening, so use it as the fingerprint that picks the
        matching JSON-LD block; fall back to it when nothing matches.
        """
        og_m = _OG_DESC_RE.search(body) or _META_DESC_RE.search(body)
        og = html_module.unescape(og_m.group(1)).strip() if og_m else None
        candidates = cls._jsonld_descriptions(body) + cls._microdata_descriptions(body)
        if not candidates:
            return og

        if og:
            og_n = cls._norm(og)
            # Compare in both directions: og may carry a price prefix the
            # JSON-LD lacks ("28.500 EUR: Ford Escort GT..."), and the OG text
            # is cut short, so whichever is shorter should appear in the other.
            for cand in sorted(candidates, key=len, reverse=True):
                cand_n = cls._norm(cand)
                head = min(60, len(og_n), len(cand_n))
                if head < 25:
                    continue
                if cand_n[:head] in og_n or og_n[:head] in cand_n:
                    return cand
            return og  # no JSON-LD block belongs to this listing

        # No OG tag to disambiguate: only trust JSON-LD when it's unambiguous.
        return candidates[0] if len(candidates) == 1 else None

    @classmethod
    def _extract_source_content(cls, body: str) -> Tuple[Optional[str], Optional[str]]:
        """(description, image) from a source listing page.

        JSON-LD first (full seller text), then OpenGraph / meta description
        as a fallback for sites that ship no structured data.
        """
        desc = cls._full_description(body)
        if desc:
            desc = html_module.unescape(desc)
            desc = re.sub(r"\s+\n", "\n", desc)
            desc = re.sub(r"[ \t]{2,}", " ", desc).strip()[:MAX_DESCRIPTION_CHARS]
        mi = _OG_IMG_RE.search(body)
        img = html_module.unescape(mi.group(1)).strip() if mi else None
        return desc or None, img or None

    # ── Parsing ─────────────────────────────────────────────────────────────
    def _build_listing(self, card: dict, list_url: str) -> Optional[Listing]:
        detail_url = card["detail_url"]
        body = self._get(detail_url, referer=list_url)
        if not body:
            return None
        try:
            title = card["title"]
            raw_price, price_val, currency = self._parse_price(card["price_raw"])

            # Full-size image on theparking's CDN - present on ~every detail
            # page, and the fallback when the source blocks us.
            im = _CLOUD_IMG_RE.search(body)
            image_url = im.group(1) if im else None

            source_url = self._resolve_source_url(body, detail_url)
            description = None
            if source_url:
                # One retry: sites like leboncoin intermittently refuse a
                # request that succeeds moments later, and without a retry
                # that listing keeps the placeholder description forever
                # (the scrape cycle never revisits a known listing).
                for attempt in range(2):
                    time.sleep(random.uniform(0.4, 0.9) + attempt * 1.5)
                    sbody = self._get(source_url, referer=detail_url)
                    if sbody:
                        description, src_img = self._extract_source_content(sbody)
                        if src_img:
                            image_url = src_img  # seller's photo beats the CDN copy
                        if description:
                            break

            domain = (re.sub(r"^https?://(www\.)?", "", source_url).split("/")[0]
                      if source_url else card.get("source_domain"))
            if not description:
                # No seller text (source blocked or has no OG tags). Say where
                # it came from so the card isn't blank; nothing to translate.
                description = (f"Listed via theparking.eu (source: {domain})"
                               if domain else "Listed via theparking.eu")

            year = None
            for y in _YEAR_RE.findall(title + " " + detail_url.replace("-", " ")):
                if 1950 <= int(y) <= 1990:
                    year = int(y)
                    break

            return Listing(
                # The real seller URL when we resolved it: the card then links
                # straight through, and an identical URL from another scraper
                # dedupes for free. theparking's own page is the fallback.
                url=source_url or detail_url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                country_code=_country_from_domain(domain),
                image_url=image_url,
                steering="unknown",
                description=description,
            )
        except Exception as exc:
            self.log.debug("theparking detail parse error %s: %s", detail_url[:70], exc)
            return None

    @staticmethod
    def _parse_price(raw: Optional[str]) -> Tuple[Optional[str], Optional[int], Optional[str]]:
        """Card price text -> (display, minor units, ISO). theparking prices
        are euro-normalised across sources; "POA" yields no value."""
        if not raw:
            return None, None, None
        digits = re.sub(r"[^\d]", "", raw)
        if not digits:
            return raw, None, None          # e.g. "POA"
        val = int(digits)
        return f"€{val:,}", val * 100, "EUR"
