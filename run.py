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

from core.currency import rates_available, usd_value
from core.database import ListingDB
from core.http_client import make_session, polite_get
from core.models import Listing
from core.notifier import EmailNotifier
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
_FINGERPRINT_STOPWORDS = {
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


def _should_keep(listing: Listing, cfg: dict, log: logging.Logger) -> bool:
    filt = cfg.get("filters", {})
    year_from = filt.get("year_from", 1968)
    year_to = filt.get("year_to", 1975)

    # Year — known years must be in range; unknown years require a Mk1 signal in title.
    if listing.year is not None:
        if not (year_from <= listing.year <= year_to):
            log.debug("Reject (year): %s", listing.url)
            return False
    else:
        title_lower = (listing.title or "").lower()
        if not any(kw in title_lower for kw in ("mk1", "mk 1", "mki", "mk i", "mark 1", "mark i")):
            log.debug("Reject (no year + no mk1 signal): %s", listing.url)
            return False

    # Steering — RHD is accepted (low-probability conversion candidate per user).
    # The digest renders "Drive: RHD" so the buyer can deprioritise visually.

    # Reject keywords
    reject_kws = filt.get("reject_title_keywords", [])
    if reject_kws and _matches_reject_keyword(listing.title or "", reject_kws):
        log.debug("Reject (title keyword): %s — %s", listing.url, listing.title)
        return False

    # USD price window
    min_usd = filt.get("min_price_usd", 2000)
    max_usd = filt.get("max_price_usd")
    if min_usd or max_usd:
        usd = usd_value(listing.price_value, listing.price_currency)
        if usd is None:
            if not rates_available():
                # Exchange-rate API is down — keep rather than blank the pipeline.
                log.warning("Keeping %s despite missing rates", listing.url)
                return True
            log.debug("Reject (no convertible price): %s", listing.url)
            return False
        if min_usd and usd < min_usd:
            log.debug("Reject (under $%d USD: $%.0f): %s", min_usd, usd, listing.url)
            return False
        if max_usd and usd > max_usd:
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


def _compute_fingerprint(listing: Listing) -> str:
    """Stable hash of the bits that identify the *car*, not the listing."""
    tokens = _title_tokens(listing.title or "")
    distinctive = sorted(t for t in tokens if t not in _FINGERPRINT_STOPWORDS)
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

    active_sites = args.sites or [
        k for k, v in sites_cfg.items() if v.get("enabled", True)
    ]

    all_new: list[Listing] = []
    log = logging.getLogger("run")

    for site_key in active_sites:
        if site_key not in SCRAPER_MAP:
            log.warning("Unknown site: %s — skipping", site_key)
            continue
        site_config = sites_cfg.get(site_key, {})
        scraper = SCRAPER_MAP[site_key](config=site_config, http_client=session)
        log.info("Running scraper: %s", site_key)

        listings = scraper._safe_fetch()
        log.info("  %d total listings fetched from %s", len(listings), site_key)

        # Drop junk before doing any further work (avoids wasted phash downloads)
        kept = [l for l in listings if _should_keep(l, cfg, log)]
        log.info("  %d passed save-time filter", len(kept))

        new = db.filter_new(kept)
        log.info("  %d new (URL-unseen)", len(new))

        # Compute phash + fingerprint, then look for cross-source duplicates.
        for l in new:
            l.image_phash = _compute_phash(l.image_url)
            l.fingerprint = _compute_fingerprint(l)
            l.canonical_url = _find_canonical(l, db, cfg)
            if l.canonical_url:
                log.info("  Duplicate: %s → canonical %s", l.url, l.canonical_url)

        if not args.check_only:
            db.save(new)

        all_new.extend(new)

    return all_new


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
        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                time.sleep(random.uniform(1.0, 2.0))
                body_text = ""
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
        _validate_http(session, db, other_urls, sold_signals, threshold)

    if fb_urls:
        _validate_facebook(cfg, db, fb_urls, sold_signals, threshold)


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
    return Listing(
        url=r["url"],
        site_name=r["site_name"],
        title=r["title"] or "",
        price=r["price"],
        price_value=r["price_value"],
        price_currency=r["price_currency"],
        year=r["year"],
        location=r["location"],
        country_code=r["country_code"],
        image_url=r["image_url"],
        steering=r["steering"],
        body_type=r["body_type"],
        description=r["description"] if "description" in r.keys() else None,
        status=r["status"],
    )


def cmd_send_digest(cfg: dict, db: ListingDB, hours: int = 24, skip_validate: bool = False) -> None:
    log = logging.getLogger("digest")
    # Always sanity-check active listings before sending so stale rows can't
    # reach the inbox. This makes the digest order-independent from cron;
    # whichever schedule fires first, the user gets fresh data.
    if not skip_validate:
        log.info("Pre-digest validation pass starting")
        try:
            run_validate(cfg, db)
        except Exception as exc:
            log.warning("Pre-digest validation failed (continuing with digest): %s", exc)

    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=hours)
    rows = db.canonical_listings_since(since)
    if not rows:
        log.info("No active canonical listings in the last %dh — nothing to send.", hours)
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
        notifier.send_digest(listings, duplicates_by_canonical)
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
    parser.add_argument("--list-db", action="store_true",
                        help="Print last 50 stored listings")
    parser.add_argument("--validate", action="store_true",
                        help="Re-check active listings; mark sold/expired")
    parser.add_argument("--serve-web", action="store_true",
                        help="Start the FastAPI + React review app on localhost:8002")
    parser.add_argument("--fb-login", action="store_true",
                        help="One-time Facebook login to save session cookies")
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

    if args.validate:
        run_validate(cfg, db)
        return

    if args.send_digest:
        cmd_send_digest(cfg, db, hours=args.hours, skip_validate=args.skip_validate)
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
