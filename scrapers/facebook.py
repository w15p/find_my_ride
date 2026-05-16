from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import List, Optional

from core.countries import country_from_display
from core.http_client import USER_AGENTS
from core.models import Listing
from scrapers.base import BaseScraper

BASE_URL = "https://www.facebook.com"
LISTING_BASE = BASE_URL + "/marketplace/item/"

LHD_KEYWORDS = {
    "lhd", "left hand drive", "left-hand drive",
    "linksgesteuert", "linksgestuurd", "gauche",
    "esquerda", "izquierda",  # Portuguese / Spanish
}


def _profile_dir(config: dict) -> str:
    return config.get("profile_dir", ".fb_profile")


class FacebookScraper(BaseScraper):
    site_name = "facebook"

    def fetch_listings(self) -> List[Listing]:
        profile_dir = _profile_dir(self.config)
        locations = self.config.get("locations", [
            {"name": "London",    "lat": 51.5074, "lng": -0.1278},
            {"name": "Amsterdam", "lat": 52.3676, "lng":  4.9041},
            {"name": "Hamburg",   "lat": 53.5753, "lng": 10.0153},
            {"name": "Brussels",  "lat": 50.8503, "lng":  4.3517},
            {"name": "Paris",     "lat": 48.8566, "lng":  2.3522},
            {"name": "Porto",     "lat": 41.1579, "lng": -8.6291},
            {"name": "Lisbon",    "lat": 38.7169, "lng": -9.1395},
            {"name": "Madrid",    "lat": 40.4168, "lng": -3.7038},
            {"name": "Barcelona", "lat": 41.3851, "lng":  2.1734},
        ])
        radius_km = self.config.get("search_radius_km", 500)

        if not Path(profile_dir).exists():
            self.log.warning(
                "No Facebook profile directory found (%s). "
                "Search results will be limited — run --fb-login first.",
                profile_dir,
            )

        all_listing_urls: set[str] = set()
        results: List[Listing] = []

        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=True,
                user_agent=random.choice(USER_AGENTS),
                viewport={"width": 1366, "height": 768},
                locale="en-GB",
            )

            search_page = ctx.new_page()
            for loc in locations:
                self.log.info("Searching Facebook near %s", loc["name"])
                urls = self._search_location(search_page, loc, radius_km)
                new_urls = urls - all_listing_urls
                all_listing_urls |= new_urls
                self.log.info("  %d new listing URLs near %s", len(new_urls), loc["name"])
                time.sleep(random.uniform(2.0, 4.0))

            detail_page = ctx.new_page()
            for listing_url in all_listing_urls:
                listing = self._fetch_detail_playwright(detail_page, listing_url)
                if listing:
                    results.append(listing)
                time.sleep(random.uniform(1.5, 3.0))

            ctx.close()

        self.log.info("Facebook total listings: %d", len(results))
        return results

    # ------------------------------------------------------------------ search

    def _search_location(self, page, loc: dict, radius_km: int) -> set[str]:
        found: set[str] = set()
        url = (
            f"{BASE_URL}/marketplace/search/"
            f"?query=ford+escort+mk1"
            f"&latitude={loc['lat']}&longitude={loc['lng']}"
            f"&radius={radius_km}&exact=false"
        )
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(3.0, 5.0))
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(random.uniform(1.5, 2.5))

            links = page.query_selector_all("a[href*='/marketplace/item/']")
            for link in links:
                href = link.get_attribute("href") or ""
                item_id = _extract_item_id(href)
                if item_id:
                    found.add(f"{LISTING_BASE}{item_id}/")
        except Exception as exc:
            self.log.warning("Facebook search failed near %s: %s", loc["name"], exc)
        return found

    # ------------------------------------------------------------------ detail via Playwright

    def _fetch_detail_playwright(self, page, listing_url: str) -> Optional[Listing]:
        item_id = _extract_item_id(listing_url)
        try:
            page.goto(listing_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(random.uniform(1.0, 2.0))
            html = page.content()

            data = _extract_relay_listing(html, item_id)
            if not data:
                self.log.debug("No relay data found for %s", listing_url)
                return None

            # Image — Facebook defers image data to a separate Relay store request,
            # so the embedded JSON in the candidate script doesn't always carry it.
            # The rendered DOM, however, has the hero image as an <img> tag inside
            # the listing's main pagelet. Pull it from there directly. This is
            # also less brittle than chasing the Relay shape across FB releases.
            dom_image = _read_dom_image(page)
            if dom_image:
                data["_image_url"] = dom_image

            # DOM fallback for price when the Relay regex missed it.
            lp = data.get("listing_price") or {}
            if not lp.get("amount"):
                dom_price = _read_dom_price(page)
                if dom_price:
                    data["listing_price"] = dom_price
                    self.log.debug("DOM-fallback price for %s: %s", listing_url, dom_price)
            elif not lp.get("currency"):
                # Have an amount but no currency — sweep the page for a symbol.
                ccy = _read_dom_currency(page)
                if ccy:
                    lp["currency"] = ccy
                    self.log.debug("DOM-fallback currency for %s: %s", listing_url, ccy)

            return self._build_listing(data, listing_url)
        except Exception as exc:
            self.log.debug("Detail fetch failed for %s: %s", listing_url, exc)
            return None

    def _build_listing(self, data: dict, url: str) -> Optional[Listing]:
        try:
            title = (
                data.get("marketplace_listing_title")
                or data.get("custom_title")
                or data.get("name")
                or ""
            )
            if not self.title_matches_search(title):
                return None

            desc = data.get("description") or data.get("redacted_description", {}).get("text", "") or ""

            # Year — title first, then description.
            year = _extract_any_year(title) or _extract_any_year(desc)

            # Steering — detect LHD keyword; don't hard-reject unknowns.
            combined = (title + " " + desc).lower()
            steering = "lhd" if any(kw in combined for kw in LHD_KEYWORDS) else "unknown"

            # Price
            price_obj = data.get("listing_price") or {}
            amount_str = price_obj.get("amount") or price_obj.get("amount_with_offset")
            currency = price_obj.get("currency")
            raw_price = None
            price_val = None
            if amount_str:
                try:
                    price_val = int(float(amount_str) * 100)
                    symbol = {"EUR": "€", "GBP": "£", "USD": "$"}.get(currency, currency or "")
                    raw_price = f"{symbol}{float(amount_str):,.0f}"
                except (ValueError, TypeError):
                    pass
            else:
                self.log.info("Facebook listing without price: %s — %s", url, title[:60])

            # Location — prefer FB's `display_name` (e.g. "Maia, Porto,
            # Portugal") because it's richer than just the city. Fall back to
            # bare city + ISO country.
            loc_obj = data.get("location") or {}
            display = loc_obj.get("display_name")
            city = loc_obj.get("city") or loc_obj.get("reverse_geocode", {}).get("city") or ""
            country = loc_obj.get("country_code") or country_from_display(display)
            location_str = display or (", ".join(filter(None, [city, (country or "").upper()])) or None)

            image_url = data.get("_image_url")

            # Trim description: collapse whitespace; cap at 1000 chars (the digest
            # template truncates to ~280 — keep the rest for debugging in DB).
            description = re.sub(r"\s+", " ", desc).strip()[:1000] or None

            return Listing(
                url=url,
                site_name=self.site_name,
                title=title,
                price=raw_price,
                price_value=price_val,
                price_currency=currency,
                year=year,
                location=location_str,
                country_code=country.upper() or None,
                image_url=image_url,
                steering=steering,
                description=description,
            )
        except Exception as exc:
            self.log.debug("Facebook listing build error: %s", exc)
            return None


# ------------------------------------------------------------------ helpers

def _decode_json_string(s: Optional[str]) -> Optional[str]:
    """Decode JSON escape sequences inside a regex-captured value.

    FB's Relay JSON ships unicode as `\\u00e7` literals; pulling a value via
    regex preserves those escapes verbatim. Run it through json.loads (with
    surrounding quotes) to get the real character (`ç`). Empty / unparseable
    strings pass through untouched so the caller never has to special-case.
    """
    if not s:
        return s
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return s


def _find_title_for_item(script: str, item_id: Optional[str]) -> Optional[str]:
    """Return the marketplace_listing_title belonging to `item_id` within `script`.

    A FB detail page's Relay payload contains many listings (the target plus
    related/recommended). Each lives in its own object with `"id":"<digits>"`
    and `"marketplace_listing_title":"..."` fields. We pair each title with
    the nearest numeric `"id"` occurrence in the script — in FB's compact JSON
    that id is virtually always the parent object's id — and return the title
    whose nearest id equals item_id. Returns None when no title can be
    confidently attributed to item_id (caller should treat this as "skip").
    """
    title_matches = list(re.finditer(r'"marketplace_listing_title"\s*:\s*"([^"]+)"', script))
    if not title_matches:
        return None
    if not item_id:
        return _decode_json_string(title_matches[0].group(1))

    id_matches = list(re.finditer(r'"id"\s*:\s*"(\d+)"', script))
    if not id_matches:
        return None

    for tm in title_matches:
        nearest = min(id_matches, key=lambda m: abs(m.start() - tm.start()))
        if nearest.group(1) == item_id:
            return _decode_json_string(tm.group(1))
    return None


def _id_window(script: str, item_id: Optional[str], radius: int = 4096) -> str:
    """Return a substring of `script` centred on the first occurrence of '"id":"<item_id>"'.

    Used to anchor independent field extractions (price, image, etc.) to the
    target listing's data block, so we don't pick up fields belonging to a
    related/suggested listing embedded in the same page.

    Returns the full script when item_id is None or not present, so callers
    still get a best-effort search.
    """
    if not item_id:
        return script
    needle = f'"id":"{item_id}"'
    idx = script.find(needle)
    if idx < 0:
        return script
    start = max(0, idx - radius)
    end = min(len(script), idx + len(needle) + radius)
    return script[start:end]


def _extract_relay_listing(html: str, item_id: Optional[str] = None) -> Optional[dict]:
    """Pull marketplace listing fields from the Relay store embedded in <script> tags."""
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.DOTALL)

    candidate_scripts = []
    for script in scripts:
        if "marketplace_listing_title" not in script:
            continue
        if item_id and item_id not in script:
            continue
        candidate_scripts.append(script)

    if not candidate_scripts:
        return None

    # Pick the script whose title we can confidently pin to the target item_id.
    # FB embeds many listings on a single detail page (target + "related" +
    # "more from seller"); the first script with `marketplace_listing_title` is
    # often a related-listings blob, not the target. Find the title whose
    # surrounding JSON object's `"id"` matches item_id, and use that script.
    script = None
    listing_title: Optional[str] = None
    for candidate in candidate_scripts:
        listing_title = _find_title_for_item(candidate, item_id)
        if listing_title:
            script = candidate
            break
    if script is None:
        # No candidate definitively matched — fall back to first script + first
        # title, but only when item_id is unknown. Bailing here when item_id is
        # set is safer than reporting a wrong title.
        if item_id:
            return None
        script = candidate_scripts[0]
        first_title_m = re.search(r'"marketplace_listing_title"\s*:\s*"([^"]+)"', script)
        if first_title_m:
            listing_title = _decode_json_string(first_title_m.group(1))

    listing_data: dict = {}
    if listing_title:
        listing_data["marketplace_listing_title"] = listing_title

    # Price — three independent searches in the window around the target item_id.
    # The fields don't have to be siblings inside the same brace pair, which
    # is what the previous strict alternations required.
    window = _id_window(script, item_id, radius=2048)
    amount_m = re.search(r'"amount"\s*:\s*"([0-9]+(?:\.[0-9]+)?)"', window)
    awo_m = re.search(r'"amount_with_offset"\s*:\s*"([0-9]+(?:\.[0-9]+)?)"', window)
    cur_m = re.search(r'"currency"\s*:\s*"([A-Z]{3})"', window)
    if amount_m or awo_m:
        listing_data["listing_price"] = {
            "amount": (amount_m or awo_m).group(1),
            "currency": cur_m.group(1) if cur_m else None,
        }

    # Description
    desc_m = re.search(r'"redacted_description"\s*:\s*\{"text"\s*:\s*"((?:[^"\\]|\\.)*)"', script)
    if not desc_m:
        desc_m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', script)
    if desc_m:
        try:
            listing_data["redacted_description"] = {
                "text": json.loads(f'"{desc_m.group(1)}"')
            }
        except Exception:
            pass

    # Location — FB's listing JSON is inconsistent: some listings carry
    # `reverse_geocode_detailed.country_alpha_two`, others only the nested
    # `reverse_geocode.city_page.display_name` like "Maia, Porto, Portugal".
    # Read whichever is present and synthesise a country code from the display
    # name if needed.
    loc_window = _id_window(script, item_id, radius=4096)
    location_obj: dict = {}
    loc_m = re.search(
        r'"latitude"\s*:\s*([\d\.\-]+).*?"longitude"\s*:\s*([\d\.\-]+)',
        loc_window, re.DOTALL,
    )
    if loc_m:
        location_obj["latitude"] = float(loc_m.group(1))
        location_obj["longitude"] = float(loc_m.group(2))
    city_m = re.search(r'"reverse_geocode"\s*:\s*\{[^{}]*"city"\s*:\s*"([^"]+)"', loc_window)
    if city_m:
        location_obj["city"] = _decode_json_string(city_m.group(1))
    display_m = re.search(r'"city_page"\s*:\s*\{[^{}]*"display_name"\s*:\s*"([^"]+)"', loc_window)
    if display_m:
        location_obj["display_name"] = _decode_json_string(display_m.group(1))
    country_m = re.search(r'"country_alpha_two"\s*:\s*"([A-Z]{2})"', loc_window)
    if country_m:
        location_obj["country_code"] = country_m.group(1)
    if location_obj:
        listing_data["location"] = location_obj

    # Image — anchored to item_id.
    # First preference: primary_listing_photo.image.uri (canonical Relay field).
    # Fallback: nearest t45.5328 CDN URI within the item_id window.
    image_window = _id_window(script, item_id, radius=4096)
    pp_m = re.search(
        r'"primary_listing_photo"\s*:\s*\{[^{}]*"image"\s*:\s*\{[^{}]*"uri"\s*:\s*"(https:[^"]+)"',
        image_window,
    )
    if pp_m:
        listing_data["_image_url"] = pp_m.group(1).replace("\\/", "/")
    else:
        cdn_m = re.search(r'"uri"\s*:\s*"(https:[^"]*t45\.5328[^"]*)"', image_window)
        if cdn_m:
            listing_data["_image_url"] = cdn_m.group(1).replace("\\/", "/")

    return listing_data if listing_data else None


def _read_dom_currency(page) -> Optional[str]:
    """Find the earliest currency-marked number in the rendered body text.

    FB's Marketplace UI puts the listing's own price near the top, and sidebar
    suggestions further down the page — so position of first occurrence is a
    reliable signal for "this listing's currency".
    """
    try:
        body_text = page.locator("body").inner_text(timeout=2000) or ""
    except Exception:
        return None
    earliest_pos = len(body_text) + 1
    chosen: Optional[str] = None
    for sym, iso in (("€", "EUR"), ("£", "GBP"), ("$", "USD")):
        m = re.search(rf"{re.escape(sym)}\s?[\d]|[\d][\d.,\s]*\s?{re.escape(sym)}", body_text)
        if m and m.start() < earliest_pos:
            earliest_pos = m.start()
            chosen = iso
    return chosen


def _read_dom_image(page) -> Optional[str]:
    """Pick the listing's hero image from the rendered DOM.

    The reliable signal isn't the CDN path (`t39.30808-6` vs `t45.5328-4` —
    FB picks different sub-paths per listing, so neither works alone) but the
    DOM **context**:

      * The listing's own gallery photos carry `alt="Product photo of <title>"`
        and live inside `aria-label="Thumbnail N"` buttons. These are the ones
        we want.
      * "Today's picks" / recommended-listing tiles carry `alt=" in <City>"`
        and an ancestor `aria-label` that begins with `, €<price>, …, listing
        <other_item_id>`. These are unrelated listings and are what previously
        bled into the captured image_url.

    We set `locale="en-GB"` in the persistent context, so the alt-text prefix
    "Product photo of" is stable. If FB ever switches locale on us, the
    `Thumbnail N` fallback still works because it's locale-independent.
    """
    try:
        src = page.evaluate("""() => {
            const imgs = Array.from(document.querySelectorAll('img'));
            // Primary: alt starts with 'Product photo of'
            for (const img of imgs) {
                if (img.src && img.src.startsWith('http')
                    && !img.src.startsWith('data:')
                    && typeof img.alt === 'string'
                    && img.alt.startsWith('Product photo of')) {
                    return img.src;
                }
            }
            // Fallback: ancestor aria-label === 'Thumbnail 1' (first gallery slot)
            for (const img of imgs) {
                let cur = img.parentElement;
                for (let d = 0; cur && d < 8; d++, cur = cur.parentElement) {
                    const a = cur.getAttribute && cur.getAttribute('aria-label');
                    if (a && /^Thumbnail 1\\b/.test(a)
                        && img.src && img.src.startsWith('http')
                        && !img.src.startsWith('data:')) {
                        return img.src;
                    }
                }
            }
            return null;
        }""")
        return src
    except Exception:
        return None


def _read_dom_price(page) -> Optional[dict]:
    """Read price from the rendered Marketplace DOM as a fallback when the Relay regex misses.

    Returns the same shape as listing_price: {"amount": str, "currency": str|None}.
    Pulls the price element's text **and a chunk of nearby body text** to give the
    currency detector enough context — FB sometimes splits the currency symbol
    from the digits into separate spans, so the direct inner_text of the price
    locator can be just "15,000" with the "€" rendered next to it.
    """
    try:
        loc = page.locator(
            '[aria-label*="price" i], [data-testid*="price" i], '
            '[class*="price"], span:has-text("€"), span:has-text("£"), span:has-text("$")'
        ).first
        if loc.count() == 0:
            return None
        text = (loc.inner_text(timeout=2000) or "").strip()
    except Exception:
        return None
    if not text:
        return None
    amount, currency = _parse_price_string(text)
    if amount is None:
        return None
    # If the local element didn't include a currency symbol, find the earliest
    # currency-marked number in the body — FB renders the listing's price near
    # the top of the page, while sidebar suggestions further down use different
    # currencies. Position-of-first-occurrence wins.
    if currency is None:
        try:
            body_text = page.locator("body").inner_text(timeout=2000) or ""
            earliest_pos = len(body_text) + 1
            for sym, iso in (("€", "EUR"), ("£", "GBP"), ("$", "USD")):
                m = re.search(rf"{re.escape(sym)}\s?[\d]|[\d][\d.,\s]*\s?{re.escape(sym)}", body_text)
                if m and m.start() < earliest_pos:
                    earliest_pos = m.start()
                    currency = iso
        except Exception:
            pass
    return {"amount": str(amount), "currency": currency}


def _parse_price_string(raw: str) -> tuple[Optional[float], Optional[str]]:
    """Best-effort parse of a 'displayed' price string into (major-units float, ISO)."""
    if not raw:
        return None, None
    currency = None
    u = raw.upper()
    if "€" in raw or "EUR" in u:
        currency = "EUR"
    elif "£" in raw or "GBP" in u:
        currency = "GBP"
    elif "$" in raw or "USD" in u:
        currency = "USD"
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None, currency
    return float(digits), currency


def _extract_item_id(url: str) -> Optional[str]:
    match = re.search(r"/marketplace/item/(\d+)", url)
    return match.group(1) if match else None


def _extract_any_year(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    match = re.search(r"\b(19[5-9]\d|200\d|201[0-9])\b", text)
    return int(match.group(1)) if match else None


def login_and_save_session(profile_dir: str) -> None:
    """Open a headed browser using the persistent profile for manual Facebook login."""
    from playwright.sync_api import sync_playwright

    print(
        "\nOpening Facebook in a browser window.\n"
        "Complete any CAPTCHA or two-factor steps as normal.\n"
        "The browser will close automatically once you are fully logged in.\n"
        f"(Profile directory: {profile_dir})\n"
    )
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()
        page.goto("https://www.facebook.com/login", wait_until="domcontentloaded")

        print("Waiting for login to complete...")
        while True:
            time.sleep(2)
            cookies = ctx.cookies()
            if any(c["name"] == "c_user" for c in cookies):
                break

        print("Logged in — profile saved.")
        time.sleep(2)
        ctx.close()
