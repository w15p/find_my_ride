from __future__ import annotations

import random
import re
import time
from typing import List, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from core.http_client import USER_AGENTS
from core.models import Listing
from scrapers.base import BaseScraper

BASE_URL = "https://www.autoscout24.com"
SEARCH_URL = (
    BASE_URL
    + "/lst/ford/escort"
    "?fregfrom=1968"
    "&fregto=1975"
    "&atype=C"
    "&cy={countries}"
    "&sort=age"    # newest first
    "&desc=1"
    "&page={page}"
)

COOKIE_CONSENT_SELECTOR = '[data-testid="as24-cmp-accept-all-button"], #as24-cmp-accept-all'
# AS24 redesigned their results UI in 2026; the listing card wrapper is now
# `[data-testid="sr-listing-card"]` (a div), no longer `<article>`. The grid
# container `sr-listing-cards-grid` wraps them.
LISTING_SELECTOR = '[data-testid="sr-listing-card"]'


class AutoScout24Scraper(BaseScraper):
    site_name = "autoscout24"

    def fetch_listings(self) -> List[Listing]:
        from playwright.sync_api import sync_playwright

        countries = self.config.get("countries", "D,NL,B,F,GB")
        max_pages = self.config.get("max_pages", 10)
        timeout = self.config.get("playwright_timeout", 45000)

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
                url = SEARCH_URL.format(countries=countries, page=page_num)
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

        self.log.info("AutoScout24 total listings: %d", len(results))
        return results

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
            if year is not None and not (1968 <= year <= 1975):
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
    match = re.search(r"\b\d{2}/(19[5-9]\d|200\d|201\d)\b", text)
    if match:
        return int(match.group(1))
    # Fallback: bare year
    match = re.search(r"\b(19[5-9]\d|200\d|201\d)\b", text)
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
