#!/usr/bin/env python3
"""
Ford Escort Mk1 LHD Monitor
============================
Usage:
  python run.py                          # Scrape → filter → dedupe → save (no email)
  python run.py --check-only             # Scrape and print new listings; no save, no email
  python run.py --sites carandclassic ebay   # Run specific sites only
  python run.py --send-digest            # Email all listings found in the last 24h
  python run.py --send-digest --hours 48 # Email listings found in the last 48h
  python run.py --list-db                # Print last 50 stored listings
  python run.py --validate               # Re-check active listings; mark sold/expired
  python run.py --serve-web              # Start React review app on localhost:8002
  python run.py --fb-login               # One-time Facebook login to save session cookies
"""
from __future__ import annotations

import argparse
import hashlib
import io
import logging
import os
import random
import re
import subprocess
import sys
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import requests
import yaml
from dotenv import load_dotenv

from core.countries import enhance_location
from core.currency import format_price, rates_available, usd_value
from core.database import ListingDB, _DEFAULT_SEARCH_LABEL, _DEFAULT_SEARCH_SLUG
from core.http_client import make_session, polite_get
from core.models import Listing
from core.notifier import EmailNotifier
from core.watchdog import OperationTimeout, watchdog
from scrapers.autoscout24 import AutoScout24Scraper
from scrapers.carandclassic import CarAndClassicScraper
from scrapers.classicdriver import ClassicDriverScraper
from scrapers.ebay import EbayScraper
from scrapers.facebook import FacebookScraper, login_and_save_session
from scrapers.marktplaats import MarktplaatsScraper

SCRAPER_MAP = {
    "carandclassic": CarAndClassicScraper,
    "classicdriver":  ClassicDriverScraper,
    "ebay":           EbayScraper,
    "marktplaats":    MarktplaatsScraper,
    "autoscout24":    AutoScout24Scraper,
    "facebook":       FacebookScraper,
}

# Per-site wall-clock budgets used by the watchdog in run_scrape. A site that
# legitimately needs longer (FB with 23-anchor sweep + 30-60s sleeps) gets
# a generous budget; the point is to catch *hangs*, not slow-but-progressing.
# Sites missing from the dict fall back to the 15min default at the call site.
_SITE_BUDGETS = {
    "facebook":      4 * 60 * 60,   # 4h — current 23-anchor rate; will tighten when option 1 ships
    "ebay":         45 * 60,        # 45m — observed 20min retry storm 2026-05-22
    "carandclassic": 10 * 60,
    "marktplaats":   10 * 60,
    "autoscout24":   15 * 60,
    "classicdriver": 10 * 60,
}

_DEFAULT_SOLD_SIGNALS = [
    "this listing is no longer available",
    "listing has been removed",
    "this item has been sold",
    "this car has been sold",
    "now sold",
    "sold subject to",
    "verkauft am",
    "reeds verkocht",
    "vendu",
    "venduto",
    "vendido",
    "vendida",
    "a venda",
    "em venda",
    "reservado",
    "reservada",
]

# Stop-words stripped before fingerprinting a title — these tokens are present
# in almost every Escort Mk1 listing and add no signal for cross-source matching.
# A different search (e.g. RS2000 seats) would use a different set; pass it
# explicitly to _compute_fingerprint via the `stopwords` parameter.
# TODO: read per-search stopwords from searches.config_json once that column exists
_FINGERPRINT_STOPWORDS: set[str] = {
    "ford", "escort", "mk1", "mki", "mk", "mark", "1", "i",
    "for", "sale", "lhd", "rhd", "two", "door", "2-door", "2dr",
    "the", "a", "of", "&", "and", "-", "lh", "rh",
}


def load_config(path: str = "config/config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    email_cfg = cfg.get("notification", {}).get("email", {})
    for key, env_var in [("smtp_user", "SMTP_USER"), ("smtp_pass", "SMTP_PASS")]:
        val = os.environ.get(env_var)
        if val:
            email_cfg[key] = val
    # Digest recipient(s) come from the env, never from the tracked YAML — keeps
    # personal email addresses out of source control. Comma-separated for
    # multiple recipients: `DIGEST_RECIPIENTS=a@x.com,b@y.com`.
    recipients = os.environ.get("DIGEST_RECIPIENTS", "").strip()
    if recipients:
        email_cfg["to_addrs"] = [a.strip() for a in recipients.split(",") if a.strip()]
    return cfg


def setup_logging(cfg: dict) -> None:
    log_cfg = cfg.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_file = log_cfg.get("file")
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=handlers,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Save-time filter — keep the DB free of obvious junk
# ──────────────────────────────────────────────────────────────────────────────

def _ascii_fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def _title_tokens(title: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _ascii_fold((title or "").lower()))


def _matches_reject_keyword(title: str, keywords: list[str]) -> bool:
    tokens = _title_tokens(title)
    folded = _ascii_fold((title or "").lower())
    # Also normalise hyphens to spaces for phrase matching so a keyword like
    # "4 door" catches "4-door" in the title without needing a separate entry.
    folded_dehyphenated = folded.replace("-", " ")
    for kw in keywords:
        kw_lower = kw.lower().strip()
        if not kw_lower:
            continue
        if " " in kw_lower:
            if kw_lower in folded or kw_lower in folded_dehyphenated:
                return True
        else:
            if kw_lower in tokens:
                return True
    return False


def _should_keep(listing: Listing, filt: dict, log: logging.Logger) -> bool:
    """Decide whether to keep a listing at save time.

    `filt` is the per-search filters dict (e.g. cfg["filters"] for the cars
    hunt during back-compat, or cfg["searches"][slug]["filters"] for any
    other search). Missing keys are treated as "no constraint" — e.g. the
    seats hunt has no year window and a much lower price floor.
    """
    year_from = filt.get("year_from")
    year_to = filt.get("year_to")

    # Year — if a year window is configured, enforce it. Listings with no
    # scraped year must carry a Mk-version signal in the title (a cars-hunt
    # heuristic to filter scale models and parts); a search with no year
    # window skips this check entirely (seats don't have meaningful years).
    if year_from is not None or year_to is not None:
        if listing.year is not None:
            if year_from is not None and listing.year < year_from:
                log.debug("Reject (year < %d): %s", year_from, listing.url)
                return False
            if year_to is not None and listing.year > year_to:
                log.debug("Reject (year > %d): %s", year_to, listing.url)
                return False
        else:
            title_lower = (listing.title or "").lower()
            if not any(kw in title_lower for kw in ("mk1", "mk 1", "mki", "mk i", "mark 1", "mark i")):
                log.debug("Reject (no year + no mk1 signal): %s", listing.url)
                return False

    # Reject keywords
    reject_kws = filt.get("reject_title_keywords") or []
    if reject_kws and _matches_reject_keyword(listing.title or "", reject_kws):
        log.debug("Reject (title keyword): %s — %s", listing.url, listing.title)
        return False

    # USD price window
    min_usd = filt.get("min_price_usd")
    max_usd = filt.get("max_price_usd")
    if min_usd is not None or max_usd is not None:
        usd = usd_value(listing.price_value, listing.price_currency)
        if usd is None:
            if not rates_available():
                # Exchange-rate API is down — keep rather than blank the pipeline.
                log.warning("Keeping %s despite missing rates", listing.url)
                return True
            log.debug("Reject (no convertible price): %s", listing.url)
            return False
        if min_usd is not None and usd < min_usd:
            log.debug("Reject (under $%d USD: $%.0f): %s", min_usd, usd, listing.url)
            return False
        if max_usd is not None and usd > max_usd:
            log.debug("Reject (over $%d USD: $%.0f): %s", max_usd, usd, listing.url)
            return False

    return True


# ──────────────────────────────────────────────────────────────────────────────
# Perceptual-hash + fingerprint for cross-source dedupe
# ──────────────────────────────────────────────────────────────────────────────

def _compute_phash(image_url: Optional[str], timeout: float = 5.0) -> Optional[str]:
    if not image_url:
        return None
    try:
        import imagehash
        import numpy as np
        from PIL import Image
        from scipy.ndimage import laplace
    except ImportError:
        return None
    # Car & Classic's signed previews need a Referer header to return 200.
    # Send it unconditionally — non-C&C servers ignore the header.
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.carandclassic.com/",
    }
    try:
        resp = requests.get(image_url, timeout=timeout, headers=headers)
        if resp.status_code != 200 or not resp.content:
            return None
        img = Image.open(io.BytesIO(resp.content))
        cropped = _auto_crop_sharp(img, np, laplace)
        return str(imagehash.phash(cropped))
    except Exception as exc:
        logging.debug("phash failed for %s: %s", image_url, exc)
        return None


def _auto_crop_sharp(img, np, laplace):
    """Detect the sharpest contiguous region of `img` and return that crop.

    C&C's signed preview URLs serve the listing photo embedded in a blurred
    "fill" background (`fill=blur&fit=fill&w=800&h=600`). The blurred padding
    has very different perceptual content from the clean photo itself, which
    bumped cross-source phash Hamming distance to ~12 for verified-duplicate
    listings.

    We compute the per-row and per-column absolute-Laplacian sum (high values
    = sharp content, low values = blur) and crop to the band where each axis's
    sharpness exceeds 60% of its mean. For a full-frame photo with no blur the
    sharpness is roughly uniform and the crop is a no-op; for the C&C preview
    it lops off the blurred sides and returns just the actual photo — making
    the phash identical to a clean version of the same shot from another site.
    """
    grey = np.asarray(img.convert("L"), dtype=np.float32)
    if grey.size == 0 or grey.shape[0] < 16 or grey.shape[1] < 16:
        return img
    sharp = np.abs(laplace(grey))
    row_sharp = sharp.sum(axis=1)
    col_sharp = sharp.sum(axis=0)

    def _band(arr):
        if arr.max() <= 0:
            return 0, len(arr) - 1
        mask = arr > arr.mean() * 0.6
        if not mask.any():
            return 0, len(arr) - 1
        idx = np.where(mask)[0]
        return int(idx.min()), int(idx.max())

    y0, y1 = _band(row_sharp)
    x0, x1 = _band(col_sharp)
    # Guard against degenerate crops: must keep at least 30% of each axis.
    h, w = grey.shape
    if (y1 - y0) < h * 0.3 or (x1 - x0) < w * 0.3:
        return img
    return img.crop((x0, y0, x1, y1))


def _compute_fingerprint(listing: Listing, stopwords: set[str] | None = None) -> str:
    """Stable hash of the bits that identify the *car*, not the listing.

    `stopwords` overrides the module-level `_FINGERPRINT_STOPWORDS` default,
    allowing callers to pass a search-specific word list without mutating global
    state. Pass `None` (the default) to use the built-in Escort Mk1 set.
    """
    effective_stopwords = _FINGERPRINT_STOPWORDS if stopwords is None else stopwords
    tokens = _title_tokens(listing.title or "")
    distinctive = sorted(t for t in tokens if t not in effective_stopwords)
    usd = usd_value(listing.price_value, listing.price_currency) or 0
    bucket = int(round(usd / 250) * 250)
    payload = f"{listing.year or 0}|{(listing.country_code or '').upper()}|{'-'.join(distinctive)}|{bucket}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _phash_distance(a: Optional[str], b: Optional[str]) -> Optional[int]:
    if not a or not b or len(a) != len(b):
        return None
    try:
        ai = int(a, 16)
        bi = int(b, 16)
    except ValueError:
        return None
    return bin(ai ^ bi).count("1")


def _find_canonical(listing: Listing, db: ListingDB, cfg: dict) -> Optional[str]:
    """Return the URL of an existing canonical row that matches `listing`, if any.

    Two match rules:
      A. Fingerprint identical (year+country+tokens+price-bucket — strongest).
      B. phash ≤ phash_max_distance AND prices within 10%.

    The phash computation auto-crops blurred padding (see `_auto_crop_sharp`)
    so cross-source duplicates whose photos share the same source asset land
    at near-zero Hamming distance even when one CDN wraps it in a `fill=blur`
    preview. That keeps the threshold tight enough to avoid false positives
    between two genuinely-different cars priced the same.
    """
    max_dist = cfg.get("filters", {}).get("phash_max_distance", 8)
    candidates = db.find_duplicate_candidates(
        year=listing.year, country_code=listing.country_code,
    )
    for row in candidates:
        if row["url"] == listing.url:
            continue
        if row["fingerprint"] and listing.fingerprint and row["fingerprint"] == listing.fingerprint:
            return row["url"]
        dist = _phash_distance(listing.image_phash, row["image_phash"])
        if dist is not None and dist <= max_dist:
            ru = usd_value(row["price_value"], row["price_currency"]) or 0
            lu = usd_value(listing.price_value, listing.price_currency) or 0
            denom = max(ru, lu, 1.0)
            if abs(ru - lu) / denom <= 0.10:
                return row["url"]
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────────────────────────────────────

def run_scrape(args, cfg: dict, db: ListingDB) -> list[Listing]:
    session = make_session()
    sites_cfg = cfg.get("sites", {})
    searches_cfg = cfg.get("searches", {})
    log = logging.getLogger("run")

    # Back-compat: a config with no `searches:` section is treated as a single
    # cars-only search. Lets older configs keep working unchanged.
    if not searches_cfg:
        from scrapers.base import DEFAULT_QUERY, DEFAULT_REQUIRED_KEYWORDS
        searches_cfg = {
            _DEFAULT_SEARCH_SLUG: {
                "label": _DEFAULT_SEARCH_LABEL,
                "query": DEFAULT_QUERY,
                "required_keywords": list(DEFAULT_REQUIRED_KEYWORDS),
                "sites": list(SCRAPER_MAP.keys()),
            }
        }

    # Resolve slug → search_id once. Skip slugs that haven't been seeded in the
    # DB (migration didn't run, or YAML config drifted from migrations).
    search_ids = {
        row["slug"]: row["id"]
        for row in db.conn.execute("SELECT id, slug FROM searches")
    }

    from core.geocode import lookup_country
    from core.translate import detect_and_translate

    all_new: list[Listing] = []
    for slug, search_cfg in searches_cfg.items():
        search_id = search_ids.get(slug)
        if search_id is None:
            log.warning("Search %r not in `searches` table — skipping. Did migrations run?", slug)
            continue

        # Per-search filters; the cars hunt falls back to the top-level
        # `filters:` block during back-compat (Commit 1 left it in place).
        search_filters = search_cfg.get("filters") or cfg.get("filters", {})

        # Per-search site list, intersected with --sites CLI override if any.
        search_sites = search_cfg.get("sites") or list(SCRAPER_MAP.keys())
        if args.sites:
            search_sites = [s for s in search_sites if s in args.sites]
        # Sites disabled in `sites:` config are honored across all searches.
        search_sites = [
            s for s in search_sites
            if sites_cfg.get(s, {}).get("enabled", True)
        ]

        log.info("=== Search: %s (id=%d) — %d site(s) ===",
                 slug, search_id, len(search_sites))

        # Per-search per-site overrides (e.g. eBay seats hunt needs a
        # `category_ids` override to escape eBay's AND-on-query-words
        # filter that hides specific listings past the page-size cutoff).
        site_overrides = search_cfg.get("site_overrides") or {}

        for site_key in search_sites:
            if site_key not in SCRAPER_MAP:
                log.warning("Unknown site: %s — skipping", site_key)
                continue
            site_config = sites_cfg.get(site_key, {})
            scraper = SCRAPER_MAP[site_key](
                config=site_config,
                http_client=session,
                query=search_cfg.get("query", "ford escort mk1"),
                required_keywords=search_cfg.get("required_keywords", ("escort",)),
                extra_params=site_overrides.get(site_key),
            )
            log.info("Running scraper: %s (search=%s)", site_key, slug)

            # Per-site watchdog budget. FB's 30-60s anchor sleep × 23 anchors
            # + detail fetches can legitimately take 3h, so it gets the lion's
            # share; eBay has been seen to hit a 20min connection-retry storm
            # (2026-05-22 cron log). Others finish in seconds.
            try:
                with watchdog(_SITE_BUDGETS.get(site_key, 15 * 60), f"scrape-{site_key}"):
                    listings = scraper._safe_fetch()
            except OperationTimeout:
                log.warning("  Site %s exceeded budget — skipping its results this tick", site_key)
                continue
            log.info("  %d total listings fetched from %s", len(listings), site_key)

            # Drop junk before doing any further work (avoids wasted phash downloads)
            kept = [l for l in listings if _should_keep(l, search_filters, log)]
            log.info("  %d passed save-time filter", len(kept))

            new = db.filter_new(kept, search_id=search_id)
            log.info("  %d new for search_id=%d", len(new), search_id)

            # Compute phash + fingerprint, then look for cross-source duplicates.
            # Also detect language and translate non-English descriptions so the
            # review-app card and digest can render English by default.
            # And fill country_code via Nominatim when the scraper missed it.
            for l in new:
                l.image_phash = _compute_phash(l.image_url)
                l.fingerprint = _compute_fingerprint(l)
                l.canonical_url = _find_canonical(l, db, cfg)
                if l.canonical_url:
                    log.info("  Duplicate: %s → canonical %s", l.url, l.canonical_url)
                if l.description:
                    l.description_language, l.description_translated = detect_and_translate(l.description)
                if l.location and not l.country_code:
                    iso = lookup_country(l.location)
                    if iso:
                        l.country_code = iso

            if not args.check_only:
                db.save(new, search_id=search_id)

            all_new.extend(new)

        # Watched URLs phase — fetch any URLs the user has pinned for this
        # search directly via the per-site detail fetcher, bypassing the
        # site's marketplace search. Catches listings that search silently
        # suppresses (e.g. FB cross-region location mismatch).
        watched_new = _process_watched_urls(args, cfg, db, slug, search_id, log)
        all_new.extend(watched_new)

    return all_new


def _process_watched_urls(
    args, cfg: dict, db: ListingDB, slug: str, search_id: int, log,
) -> list[Listing]:
    """Fetch all watched URLs for a search, dispatch by site, save results.

    Returns the list of newly-saved Listings so the caller can fold them
    into all_new. Watched URLs intentionally bypass save-time filters
    (`_should_keep`, title match) — the user has explicitly opted in by
    pinning the URL, so filters from search-discovery don't apply.
    """
    rows = db.list_watched_urls(search_id=search_id)
    if not rows:
        return []

    fb_urls: list[str] = []
    fb_id_by_url: dict[str, int] = {}
    for row in rows:
        url = row["url"]
        if "facebook.com/marketplace" in url:
            fb_urls.append(url)
            fb_id_by_url[url] = row["id"]
        else:
            db.mark_watched_url_fetched(row["id"], "unsupported_site")
            log.info("  Watched URL site not yet supported: %s", url)

    if not fb_urls:
        return []

    log.info("=== Watched URLs (search=%s): fetching %d FB URL(s) ===",
             slug, len(fb_urls))

    from scrapers.facebook import fetch_watched_listings
    profile_dir = _fb_profile_dir(cfg)
    # ~30s per URL realistic; 30min absolute ceiling protects against a
    # Playwright goto hang on a single bad URL stalling the whole tick.
    try:
        with watchdog(30 * 60, "watched-urls-fetch"):
            fetched = fetch_watched_listings(fb_urls, profile_dir, log)
    except OperationTimeout:
        log.warning("Watched-URL fetch exceeded 30min — skipping for this tick")
        return []

    new_listings: list[Listing] = []
    for url, listing, status in fetched:
        watched_id = fb_id_by_url[url]
        db.mark_watched_url_fetched(watched_id, status)
        if not listing:
            log.info("  Watched fetch failed: %s", url)
            continue
        # Already in DB and tagged with this search? Skip silently — the
        # watched URL is still useful as a retry-on-future-suppression
        # safety net, but we don't double-process listings.
        existing = db.filter_new([listing], search_id=search_id)
        if not existing:
            log.debug("  Watched URL already tracked: %s", url)
            continue
        if not args.check_only:
            db.save([listing], search_id=search_id)
        new_listings.append(listing)
        log.info("  Watched URL captured: %s — %s", url, listing.title[:60])

    return new_listings


def _has_word_boundary_signal(body_lower: str, sig: str) -> bool:
    """Match `sig` in `body_lower` with word boundaries for single tokens,
    substring for multi-word phrases."""
    if " " in sig:
        return sig in body_lower
    return re.search(rf"\b{re.escape(sig)}\b", body_lower) is not None


def _is_sold_by_text(body: str, signals: list[str]) -> bool:
    body_lower = body.lower()
    return any(_has_word_boundary_signal(body_lower, s.lower()) for s in signals)


def _fb_profile_dir(cfg: dict) -> str:
    return cfg.get("sites", {}).get("facebook", {}).get("profile_dir", ".fb_profile")


# Facebook Marketplace renders a sold listing's status as the bare word "Sold"
# on its own line, immediately before the listing title. (Generic substring
# matching of "sold" against the full page is what the old --validate did; it
# hit "Buy and sell groups", "Sold by …", footer copy, etc. — this pattern is
# specific to FB's status badge.)
_FB_SOLD_LINE = re.compile(r"^Sold\s*$", re.MULTILINE)


def cmd_mine_suggestions(cfg: dict, db: ListingDB, search_slug: Optional[str] = None) -> None:
    """Run the Tier 1 pattern miner; writes to the suggestions table.

    Without --search-slug, mines every search. Idempotent — duplicate
    suggestions (same search_id + kind + value) silently no-op via the
    UNIQUE constraint on the suggestions table.
    """
    from core.miner import mine_for_search, mine_all_searches
    log = logging.getLogger("mine")
    if search_slug:
        row = db.conn.execute(
            "SELECT id FROM searches WHERE slug = ?", (search_slug,)
        ).fetchone()
        if not row:
            log.error("Unknown search slug %r — nothing to mine.", search_slug)
            return
        counts = mine_for_search(db, row["id"])
        log.info("Mined %r: %s", search_slug, counts)
    else:
        results = mine_all_searches(db)
        for slug, counts in results.items():
            log.info("Mined %r: %s", slug, counts)


def cmd_refresh_fb_images(cfg: dict, db: ListingDB) -> None:
    """Re-fetch the hero image URL for every active Facebook listing.

    Facebook signs CDN image URLs with a ~24-48h TTL (`oe=` query param);
    after expiry the URL returns 403 and the review UI shows broken images.
    The normal scrape cycle never refreshes URLs for already-known listings
    (filter_new drops them), so without this command stored URLs go stale
    within a day or two. Cron-friendly: re-run as often as desired.
    """
    log = logging.getLogger("refresh.fb")
    rows = db.conn.execute(
        "SELECT url FROM listings WHERE site_name='facebook' AND status='active'"
    ).fetchall()
    urls = [r["url"] for r in rows]
    if not urls:
        log.info("No active Facebook listings to refresh.")
        return

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log.error("Playwright not available — cannot refresh: %s", exc)
        return
    # Import here so the heavy `scrapers.facebook` module isn't loaded for
    # CLI commands that don't need it.
    from scrapers.facebook import _read_dom_image
    from core import image_cache

    profile_dir = _fb_profile_dir(cfg)
    updated = unchanged = failed = prefetched = 0
    log.info("Refreshing image URLs for %d Facebook listing(s)", len(urls))
    # ~3-5s per listing realistic; cap at 45min as a bound against a runaway
    # loop (e.g. Playwright pipe IO hanging mid-batch). Partial progress is
    # safe to commit — each db.update_image_url is its own auto-commit.
    try:
        with watchdog(45 * 60, "refresh-fb-images"):
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    user_data_dir=profile_dir,
                    headless=True,
                    viewport={"width": 1366, "height": 768},
                    locale="en-GB",
                )
                page = ctx.new_page()
                for i, url in enumerate(urls, 1):
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(random.uniform(1.0, 2.0))
                        new_img = _read_dom_image(page)
                        if not new_img:
                            failed += 1
                            log.warning("[%d/%d] No image extracted from %s", i, len(urls), url)
                            continue
                        current = db.conn.execute(
                            "SELECT image_url FROM listings WHERE url=?", (url,)
                        ).fetchone()
                        if current and current["image_url"] == new_img:
                            unchanged += 1
                        else:
                            db.update_image_url(url, new_img)
                            updated += 1
                            log.info("[%d/%d] Updated: %s", i, len(urls), url[:80])
                        # Prefetch the bytes into the proxy cache so the
                        # next view loads instantly + survives the next FB
                        # CDN expiry. Best-effort: failures are debug-only.
                        if _prefetch_image_to_cache(new_img):
                            prefetched += 1
                        time.sleep(random.uniform(1.0, 2.0))
                    except Exception as exc:
                        failed += 1
                        log.warning("[%d/%d] Failed for %s: %s", i, len(urls), url, exc)
                ctx.close()
    except OperationTimeout:
        log.warning("FB image refresh exceeded 45min — committed %d of %d so far",
                    updated + unchanged + failed, len(urls))
    log.info(
        "Refresh complete: %d updated, %d unchanged, %d failed, %d prefetched (of %d total)",
        updated, unchanged, failed, prefetched, len(urls),
    )


def _prefetch_image_to_cache(img_url: str) -> bool:
    """Download an image URL into the proxy disk cache. Returns True iff a
    new cache entry was written. Skips silently if already cached or on any
    fetch/write failure — this is best-effort warming, not a critical path.
    """
    from core import image_cache
    log = logging.getLogger("refresh.fb")
    if image_cache.find(img_url):
        return False
    try:
        r = requests.get(
            img_url,
            timeout=10,
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )
        if r.status_code != 200:
            log.debug("Prefetch %d for %s — skipping cache", r.status_code, img_url)
            return False
        content_type = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip().lower()
        image_cache.write(img_url, content_type, r.iter_content(8192))
        return True
    except Exception as exc:
        log.debug("Prefetch failed for %s: %s", img_url, exc)
        return False


def _is_fb_sold(body_text: str, generic_signals: list[str]) -> bool:
    if _FB_SOLD_LINE.search(body_text):
        return True
    return _is_sold_by_text(body_text, generic_signals)


def _validate_facebook(cfg: dict, db: ListingDB, urls: list[str], sold_signals: list[str], threshold: int) -> None:
    """Validate Facebook listings inside the authenticated Playwright context.

    `requests` cannot fetch FB listing pages — they return a login wall whose
    boilerplate text triggers false "sold" matches. Playwright with the
    persistent profile sees the real page.
    """
    log = logging.getLogger("validate.fb")
    if not urls:
        return
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log.warning("Playwright not available — skipping FB validation: %s", exc)
        return

    profile_dir = _fb_profile_dir(cfg)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=True,
            viewport={"width": 1366, "height": 768},
            locale="en-GB",
        )
        page = ctx.new_page()
        # Bail early if the FB session has been invalidated. Without this
        # check, every listing-validation page.goto hits a login wall, hangs
        # waiting for content that never appears, and the cron process
        # holds the DB write lock — blocking reject/pin/note in the webapp.
        # Pattern caught after FB's 2026-05-19 forced logout.
        from scrapers.facebook import _check_session_valid
        if not _check_session_valid(page, log):
            ctx.close()
            return

        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(1.0, 2.0))
                # Scope the "Sold" text scan to the main listing pane only.
                # FB's sidebar shows "More from this seller" / "Related
                # listings" tiles, and those tiles render a literal "Sold"
                # badge for the seller's other sold items — which the
                # whole-page regex was incorrectly attributing to the target
                # listing. Same anchor-to-target bug class as the FB title
                # attribution fix from commit eff40b8.
                body_text = ""
                try:
                    body_text = page.locator('[role="main"]').inner_text(timeout=5000) or ""
                except Exception:
                    # FB may change the role attr — fall back to whole-body
                    # so we don't silently stop detecting sold listings.
                    # Risk: re-introduces the sidebar FP; acceptable as a
                    # rare fallback rather than a default.
                    try:
                        body_text = page.locator("body").inner_text(timeout=5000) or ""
                    except Exception:
                        body_text = page.content()
                if _is_fb_sold(body_text, sold_signals):
                    new_count = db.increment_sold_signal(url)
                    if new_count >= threshold:
                        log.info("Marking sold (FB, strike %d): %s", new_count, url)
                        db.mark_status(url, "sold")
                    else:
                        log.info("Sold signal #%d (below threshold) for %s", new_count, url)
                else:
                    db.reset_sold_signal(url)
            except Exception as exc:
                # FB 404s render as a generic error page — treat as expired.
                msg = str(exc).lower()
                if "404" in msg or "not found" in msg:
                    log.info("Marking expired (FB 404): %s", url)
                    db.mark_status(url, "expired")
                else:
                    log.debug("FB validate failed for %s: %s", url, exc)
        ctx.close()


def _validate_http(session, db: ListingDB, urls: list[str], sold_signals: list[str], threshold: int) -> None:
    log = logging.getLogger("validate.http")
    for url in urls:
        try:
            resp = polite_get(session, url, min_delay=1.0, max_delay=3.0)
            if _is_sold_by_text(resp.text, sold_signals):
                new_count = db.increment_sold_signal(url)
                if new_count >= threshold:
                    log.info("Marking sold (strike %d): %s", new_count, url)
                    db.mark_status(url, "sold")
                else:
                    log.info("Sold signal #%d (below threshold) for %s", new_count, url)
            else:
                db.reset_sold_signal(url)
        except Exception as exc:
            status_code = getattr(exc.response, "status_code", None) if hasattr(exc, "response") else None
            if status_code == 404:
                log.info("Marking expired (404): %s", url)
                db.mark_status(url, "expired")
            else:
                log.debug("Could not validate %s: %s", url, exc)


def run_validate(cfg: dict, db: ListingDB) -> None:
    log = logging.getLogger("validate")
    sold_signals = cfg.get("validate", {}).get("sold_signals") or _DEFAULT_SOLD_SIGNALS
    threshold = int(cfg.get("validate", {}).get("sold_strike_threshold", 2))

    active = db.active_listings()
    log.info("Validating %d active listings...", len(active))

    fb_urls = [r["url"] for r in active if r["site_name"] == "facebook"]
    other_urls = [r["url"] for r in active if r["site_name"] != "facebook"]

    if other_urls:
        session = make_session()
        try:
            with watchdog(30 * 60, "validate-http"):
                _validate_http(session, db, other_urls, sold_signals, threshold)
        except OperationTimeout:
            log.warning("HTTP validate exceeded 30min — moving on to FB validate")

    if fb_urls:
        try:
            with watchdog(60 * 60, "validate-facebook"):
                _validate_facebook(cfg, db, fb_urls, sold_signals, threshold)
        except OperationTimeout:
            log.warning("FB validate exceeded 60min — aborting validate phase")


def cmd_list_db(db: ListingDB) -> None:
    rows = db.recent_listings()
    if not rows:
        print("No listings in database.")
        return
    print(f"{'Scraped':>20}  {'Site':15}  {'Year':5}  {'Status':8}  {'Price':>10}  Title")
    print("-" * 90)
    for row in rows:
        print(
            f"{row['scraped_at'][:19]:>20}  "
            f"{row['site_name']:15}  "
            f"{str(row['year'] or '?'):5}  "
            f"{row['status']:8}  "
            f"{str(row['price'] or 'POA'):>10}  "
            f"{(row['title'] or '')[:50]}"
        )


def _row_to_listing(r) -> Listing:
    # Honour user overrides when building a Listing for the digest, so the
    # email displays the corrected currency (and the USD conversion uses the
    # corrected ISO code). Year/location/steering overrides flow through the
    # same way.
    keys = r.keys()
    eff_currency = (r["user_price_currency"] if "user_price_currency" in keys else None) or r["price_currency"]
    eff_price_str = format_price(r["price_value"], eff_currency) or r["price"]
    eff_year = (r["user_year"] if "user_year" in keys else None) or r["year"]
    eff_loc = (
        (r["user_location"] if "user_location" in keys else None)
        or enhance_location(r["location"], r["country_code"] if "country_code" in keys else None)
    )
    eff_steering = (r["user_steering"] if "user_steering" in keys else None) or r["steering"]
    return Listing(
        url=r["url"],
        site_name=r["site_name"],
        title=r["title"] or "",
        price=eff_price_str,
        price_value=r["price_value"],
        price_currency=eff_currency,
        year=eff_year,
        location=eff_loc,
        country_code=r["country_code"],
        image_url=r["image_url"],
        steering=eff_steering,
        body_type=r["body_type"],
        description=r["description"] if "description" in keys else None,
        status=r["status"],
    )


def cmd_send_digest(
    cfg: dict,
    db: ListingDB,
    hours: int = 24,
    skip_validate: bool = False,
    search_slug: str = _DEFAULT_SEARCH_SLUG,
) -> None:
    log = logging.getLogger("digest")

    # Resolve the search slug to id + label up front. A missing row almost
    # certainly means the migration didn't run or the slug was mistyped on
    # the CLI; bail cleanly rather than send an empty email.
    search_row = db.conn.execute(
        "SELECT id, label FROM searches WHERE slug = ?", (search_slug,)
    ).fetchone()
    if not search_row:
        log.error("Unknown search slug %r — nothing to digest.", search_slug)
        return
    search_id = search_row["id"]
    search_label = search_row["label"]

    # Always sanity-check active listings before sending so stale rows can't
    # reach the inbox. This makes the digest order-independent from cron;
    # whichever schedule fires first, the user gets fresh data. Validation is
    # search-agnostic (sold is sold regardless of which search a listing
    # belongs to) so we don't filter it by search_id.
    if not skip_validate:
        log.info("Pre-digest validation pass starting")
        try:
            run_validate(cfg, db)
        except Exception as exc:
            log.warning("Pre-digest validation failed (continuing with digest): %s", exc)

    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
    rows = db.canonical_listings_since(since, search_id=search_id)
    if not rows:
        log.info(
            "No active canonical listings for %r in the last %dh — nothing to send.",
            search_slug, hours,
        )
        return

    listings = [_row_to_listing(r) for r in rows]
    duplicates_by_canonical: dict[str, list[Listing]] = {}
    for r in rows:
        dups = db.duplicates_of(r["url"])
        if dups:
            duplicates_by_canonical[r["url"]] = [_row_to_listing(d) for d in dups]

    notif_cfg = cfg.get("notification", {})
    if notif_cfg.get("type") == "email":
        notifier = EmailNotifier(notif_cfg.get("email", {}))
        notifier.send_digest(listings, duplicates_by_canonical, search_label=search_label)
        log.info("Digest sent: %d canonical listing(s) from the last %dh", len(listings), hours)
    else:
        log.warning("No email notifier configured.")


def cmd_serve_web(host: str = "127.0.0.1", port: int = 8002) -> None:
    """Run the FastAPI review app. Serves the built React frontend if present,
    otherwise the API alone for the Vite dev server to proxy to."""
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("fastapi / uvicorn missing. Run: pip install -r requirements.txt")
        sys.exit(1)
    web_dist = Path(__file__).parent / "webapp" / "web" / "dist"
    if not web_dist.exists():
        print("(!) Frontend not built. From webapp/web run `npm install && npm run dev`")
        print(f"    to develop, or `npm run build` to produce {web_dist}.")
        print(f"    API endpoints will still be reachable at http://{host}:{port}/api/*")
    print(f"Review app: http://{host}:{port}")
    try:
        subprocess.run(
            [sys.executable, "-m", "uvicorn", "webapp.api:app",
             "--host", host, "--port", str(port)],
            check=True,
        )
    except KeyboardInterrupt:
        pass


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Ford Escort Mk1 LHD Monitor")
    parser.add_argument("--check-only", action="store_true",
                        help="Scrape and print new listings without saving or notifying")
    parser.add_argument("--sites", nargs="+", choices=list(SCRAPER_MAP.keys()),
                        help="Run specific sites only")
    parser.add_argument("--send-digest", action="store_true",
                        help="Email all listings found in the last --hours hours (default 24)")
    parser.add_argument("--hours", type=int, default=24,
                        help="Hours window for --send-digest (default: 24)")
    parser.add_argument("--skip-validate", action="store_true",
                        help="With --send-digest: skip the pre-digest validation pass")
    parser.add_argument("--search-slug", default=None,
                        help="With --send-digest: which saved search to digest "
                             f"(default: {_DEFAULT_SEARCH_SLUG}). "
                             "With --mine-suggestions: which search to mine "
                             "(default: all searches).")
    parser.add_argument("--list-db", action="store_true",
                        help="Print last 50 stored listings")
    parser.add_argument("--validate", action="store_true",
                        help="Re-check active listings; mark sold/expired")
    parser.add_argument("--serve-web", action="store_true",
                        help="Start the FastAPI + React review app on localhost:8002")
    parser.add_argument("--fb-login", action="store_true",
                        help="One-time Facebook login to save session cookies")
    parser.add_argument("--refresh-fb-images", action="store_true",
                        help="Re-fetch image URLs for active Facebook listings "
                             "(FB CDN URLs expire after ~24-48h)")
    parser.add_argument("--mine-suggestions", action="store_true",
                        help="Run the pattern miner over rejects + keeps for "
                             "every search (or just --search-slug if given). "
                             "Writes to the suggestions table for UI review.")
    parser.add_argument("--config", default="config/config.yaml",
                        help="Path to config.yaml (default: config/config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)
    db = ListingDB(cfg.get("database", {}).get("path", "listings.db"))

    if args.list_db:
        cmd_list_db(db)
        return

    if args.serve_web:
        cmd_serve_web()
        return

    if args.fb_login:
        profile_dir = _fb_profile_dir(cfg)
        login_and_save_session(profile_dir)
        return

    if args.refresh_fb_images:
        cmd_refresh_fb_images(cfg, db)
        return

    if args.mine_suggestions:
        # No slug = mine all searches; slug = mine just that one.
        cmd_mine_suggestions(cfg, db, search_slug=args.search_slug)
        return

    if args.validate:
        run_validate(cfg, db)
        return

    if args.send_digest:
        cmd_send_digest(
            cfg, db,
            hours=args.hours,
            skip_validate=args.skip_validate,
            search_slug=args.search_slug or _DEFAULT_SEARCH_SLUG,
        )
        return

    all_new = run_scrape(args, cfg, db)

    if args.check_only:
        if all_new:
            print(f"\n{len(all_new)} new listing(s) found:\n")
            for l in all_new:
                marker = "[DUP]" if l.canonical_url else "     "
                print(
                    f"  {marker} [{l.site_name:15}] {l.year or '?'}  "
                    f"{(l.price or 'POA'):>10}  {l.title[:60]}"
                )
                print(f"          {l.url}")
                if l.description:
                    print(f"          {l.description[:120]}")
        else:
            print("No new listings found.")
        return

    if all_new:
        logging.info("%d new listing(s) saved. Run --send-digest to email them.", len(all_new))
    else:
        logging.info("No new listings found.")


if __name__ == "__main__":
    main()
