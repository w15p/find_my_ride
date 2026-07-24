from __future__ import annotations

import random
import re
import time
from typing import List, Optional
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from core.http_client import USER_AGENTS
from core.models import Listing, MAX_DESCRIPTION_CHARS
from scrapers.base import BaseScraper

BASE_URL = "https://www.autoscout24.com"
# AutoScout24 organizes its catalogue by make/model URL slugs:
#   /lst/<make>/<model>?...   e.g. /lst/ford/escort
# We derive those slugs from the search query so any hunt, not just the
# original Ford Escort, can drive this scraper.
#
# Two-word makes need an explicit map so "alfa romeo" is not split into
# make="alfa", model="romeo". Common abbreviations are aliased too.
_MULTI_WORD_MAKES = {
    "alfa romeo": "alfa-romeo",
    "aston martin": "aston-martin",
    "land rover": "land-rover",
    "mercedes benz": "mercedes-benz",
    "rolls royce": "rolls-royce",
    "great wall": "great-wall",
}
_MAKE_ALIASES = {
    "alfa": "alfa-romeo",
    "mercedes": "mercedes-benz",
    "merc": "mercedes-benz",
    "vw": "volkswagen",
    "chevy": "chevrolet",
    "beemer": "bmw",
    "bimmer": "bmw",
}
# Tokens that may follow a make but never name an AS24 model slug (drive side,
# Mk-generation marks). Keeps a junk segment out of the URL; these still drive
# title filtering via required_keywords.
_NON_MODEL_TOKENS = {
    "lhd", "rhd", "left", "right", "hand", "drive",
    "mk1", "mki", "mk2", "mkii", "mk3", "mkiii",
}


def _slug(token: str) -> str:
    """Lowercase a token to an AS24 URL slug (alnum runs joined by hyphens)."""
    return re.sub(r"[^a-z0-9]+", "-", token.lower()).strip("-")


def _parse_make_model(query: str) -> tuple[str, Optional[str]]:
    """Derive (make_slug, model_slug) from a freeform query string.

    make = the longest leading match against known two-word makes, else the
    first token (alias-resolved). model = the next token when it plausibly
    names a model, else None (a make-only listing page, refined afterwards by
    required_keywords title filtering). Only the single token after the make
    is taken as the model, so multi-token models (e.g. BMW "3 series",
    Mercedes "280 SL") lose their trailing words - set make/model explicitly
    via site_overrides for those.
    """
    tokens = [t for t in re.split(r"\s+", query.strip().lower()) if t]
    if not tokens:
        return "", None
    if len(tokens) >= 2 and f"{tokens[0]} {tokens[1]}" in _MULTI_WORD_MAKES:
        make = _MULTI_WORD_MAKES[f"{tokens[0]} {tokens[1]}"]
        consumed = 2
    else:
        make = _MAKE_ALIASES.get(tokens[0], _slug(tokens[0]))
        consumed = 1
    model = None
    if len(tokens) > consumed and tokens[consumed] not in _NON_MODEL_TOKENS:
        model = _slug(tokens[consumed])
    return make, model


def _build_search_url(
    make: str,
    model: Optional[str],
    countries: str,
    year_from: Optional[int],
    year_to: Optional[int],
    page: int,
) -> str:
    path = f"/lst/{make}/{model}" if model else f"/lst/{make}"
    # AS24 moved from comma-joined `cy=D,NL,B,F,GB` to REPEATED `cy=D&cy=NL&...`
    # sometime in mid-2026. The old form returns zero results (which was the
    # actual cause of "0 total listings" from every scrape until this fix).
    # Emit one cy=X per country by using a list of tuples instead of a dict.
    params: list[tuple[str, str]] = []
    if year_from is not None:
        params.append(("fregfrom", str(year_from)))
    if year_to is not None:
        params.append(("fregto", str(year_to)))
    params.append(("atype", "C"))
    for cc in (c.strip() for c in countries.split(",") if c.strip()):
        params.append(("cy", cc))
    params.append(("sort", "age"))   # newest first
    params.append(("desc", "1"))
    params.append(("page", str(page)))
    return BASE_URL + path + "?" + urlencode(params)

COOKIE_CONSENT_SELECTOR = '[data-testid="as24-cmp-accept-all-button"], #as24-cmp-accept-all'
# AS24 redesigned again mid-2026: the listing card wrapper is now
# `<article data-testid="...">` (they went back to the `<article>` element
# they used before the 2025 redesign). The previous `sr-listing-card`
# testid returns zero elements now, which had been silently making every
# AS24 scrape return "0 total listings" without any error signal.
LISTING_SELECTOR = 'article[data-testid]'


class AutoScout24Scraper(BaseScraper):
    site_name = "autoscout24"

    def fetch_listings(self) -> List[Listing]:
        from playwright.sync_api import sync_playwright

        countries = self.config.get("countries", "D,NL,B,F,GB")
        max_pages = self.config.get("max_pages", 10)
        timeout = self.config.get("playwright_timeout", 45000)

        # Derive the AS24 make/model slugs from the search query so this
        # scraper follows whatever hunt is configured, not just Ford Escort.
        # Explicit site_overrides (make/model/year_from/year_to) win over the
        # parsed query; the year window otherwise falls back to site config.
        make = self.extra_params.get("make") or ""
        model = self.extra_params.get("model")
        if not make:
            make, parsed_model = _parse_make_model(self.query)
            if model is None:
                model = parsed_model
        if not make:
            self.log.warning(
                "AutoScout24: no make derivable from query %r - skipping site",
                self.query,
            )
            return []
        self._year_from = self.extra_params.get("year_from", self.config.get("year_from"))
        self._year_to = self.extra_params.get("year_to", self.config.get("year_to"))
        year_from, year_to = self._year_from, self._year_to
        self.log.info(
            "AutoScout24 search: make=%s model=%s years=%s-%s",
            make, model or "(any)",
            year_from if year_from is not None else "",
            year_to if year_to is not None else "",
        )

        results: List[Listing] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1366, "height": 768},
                locale="en-GB",
            )
            page = context.new_page()

            for page_num in range(1, max_pages + 1):
                url = _build_search_url(make, model, countries, year_from, year_to, page_num)
                self.log.info("AutoScout24 page %d: %s", page_num, url)

                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                except Exception as exc:
                    self.log.warning("Navigation failed on page %d: %s", page_num, exc)
                    break

                # Accept cookie consent banner (first page only)
                if page_num == 1:
                    try:
                        page.click(COOKIE_CONSENT_SELECTOR, timeout=5000)
                        time.sleep(1)
                    except Exception:
                        pass

                # Wait for listing cards to appear
                try:
                    page.wait_for_selector(LISTING_SELECTOR, timeout=15000)
                except Exception:
                    self.log.info("No listings found on page %d — stopping", page_num)
                    break

                cards = page.query_selector_all(LISTING_SELECTOR)
                if not cards:
                    self.log.info("Empty page %d — stopping", page_num)
                    break

                # Scroll each card into view to trigger the lazy-loaded image,
                # otherwise the <img> src is a placeholder at read time.
                for card in cards:
                    try:
                        card.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                time.sleep(1.5)

                for card in cards:
                    listing = self._parse_card(card)
                    if listing:
                        results.append(listing)

                # Polite delay between pages
                time.sleep(random.uniform(3.0, 7.0))

                # Check for "no more results"
                if len(cards) < 20 and page_num > 1:
                    break

            browser.close()

        # Enrich each result with the seller's free-text description from the
        # detail page. AS24 embeds it in a Next.js hydration blob so a plain
        # requests fetch is enough - no need for a second Playwright pass.
        # The translation pipeline in run.py (detect_and_translate) picks it
        # up from listing.description afterward.
        for listing in results:
            desc = self._fetch_description(listing.url)
            if desc:
                listing.description = desc[:MAX_DESCRIPTION_CHARS]
            # Polite: AS24 rate-limits on rapid detail hits from a single IP.
            time.sleep(random.uniform(0.4, 1.0))

        self.log.info("AutoScout24 total listings: %d", len(results))
        return results

    def _fetch_description(self, url: str) -> Optional[str]:
        """Return the seller's free-text description from an AS24 detail page.

        The description lives in the Next.js __NEXT_DATA__ script at
        `props.pageProps.listingDetails.description` and comes wrapped in
        light HTML (<strong>, <br />). We strip the tags to plain text so the
        translator gets clean input.
        """
        try:
            resp = self.http.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=20)
            if resp.status_code != 200:
                self.log.debug("AS24 detail %d for %s", resp.status_code, url)
                return None
            import json
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>', resp.text)
            if not m:
                return None
            data = json.loads(m.group(1))
            desc = (
                data.get("props", {})
                .get("pageProps", {})
                .get("listingDetails", {})
                .get("description")
            )
            if not desc:
                return None
            # Strip HTML tags and collapse whitespace.
            text = re.sub(r"<br\s*/?>", "\n", desc)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            return text or None
        except Exception as exc:
            self.log.debug("AS24 detail fetch failed for %s: %s", url, exc)
            return None

    def _parse_card(self, card) -> Optional[Listing]:
        try:
            link = card.query_selector("a[href*='/offers/']")
            if not link:
                return None
            href = link.get_attribute("href") or ""
            url = urljoin(BASE_URL, href) if href.startswith("/") else href
            url = _strip_query(url)
            if not url:
                return None

            # Title — used to be <h2>, redesign moved it to <h3>. Match either
            # so this scraper survives a back-and-forth UI change.
            title_el = card.query_selector("h2, h3")
            title = title_el.inner_text().strip() if title_el else ""
            if not self.title_matches_search(title):
                return None

            # Parse year from the card's full text — AutoScout24 shows "MM/YYYY"
            full_text = card.inner_text()
            year = _extract_year_from_text(full_text)
            if year is not None:
                if self._year_from is not None and year < self._year_from:
                    return None
                if self._year_to is not None and year > self._year_to:
                    return None

            # Price
            price_el = card.query_selector("[data-testid*='price'], [class*='price']")
            raw_price = price_el.inner_text().strip() if price_el else None
            price_val, currency = _parse_price_string(raw_price)

            # Location — last meaningful line of text
            location = _extract_location(full_text)

            # Image — AS24 serves the listing's hero photo at
            # https://prod.pictures.autoscout24.net/listing-images/{listing_uuid}_
            # {photo_uuid}.jpg/{size}.webp. The card defaults to 250x188 (the
            # search-results thumbnail); we swap the trailing size segment for
            # 720x540 so the review-app card has a real photo, not a postage stamp.
            img_el = card.query_selector("img")
            image_url = img_el.get_attribute("src") if img_el else None
            if image_url and image_url.startswith("data:"):
                image_url = None
            if image_url:
                image_url = re.sub(
                    r"/\d+x\d+\.(webp|jpg|jpeg|png)$",
                    "/720x540.webp",
                    image_url,
                )

            # Synthetic description: pick the first informative card line that's
            # not the title or price (typically the model/spec subheading).
            description = _extract_subtitle(full_text, title, raw_price)

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
                steering="unknown",
                description=description,
            )
        except Exception as exc:
            self.log.debug("AutoScout24 card parse error: %s", exc)
            return None


def _extract_year_from_text(text: str) -> Optional[int]:
    """AutoScout24 shows registration as MM/YYYY — extract the year."""
    match = re.search(r"\b\d{2}/(19[5-9]\d|20[0-2]\d)\b", text)
    if match:
        return int(match.group(1))
    # Fallback: bare year
    match = re.search(r"\b(19[5-9]\d|20[0-2]\d)\b", text)
    return int(match.group(1)) if match else None


def _extract_location(text: str) -> Optional[str]:
    """Find country-code prefixed location line e.g. 'NL-9101 WV Dokkum'."""
    match = re.search(r"\b([A-Z]{2})-[\w\s]+$", text, re.MULTILINE)
    return match.group(0).strip() if match else None


def _parse_price_string(raw: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    if not raw:
        return None, None
    currency = None
    if "€" in raw or "EUR" in raw:
        currency = "EUR"
    elif "£" in raw or "GBP" in raw:
        currency = "GBP"
    digits = re.sub(r"[^\d]", "", raw)
    if digits:
        return int(digits) * 100, currency  # convert to minor units
    return None, currency


def _strip_query(url: str) -> str:
    """Drop query string and fragment so session-tagged URLs dedupe correctly."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _extract_subtitle(full_text: str, title: str, price: Optional[str]) -> Optional[str]:
    """Return the most informative card line that isn't the title or price."""
    title_norm = (title or "").strip()
    price_norm = (price or "").strip()
    for line in (full_text or "").splitlines():
        line = line.strip()
        if not line or line == title_norm or line == price_norm:
            continue
        if len(line) < 6:
            continue
        return line[:280]
    return None
