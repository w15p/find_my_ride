"""Tier 1 pattern miner — scans rejects + keeps for a saved search and
writes suggestions to the `suggestions` table for the user to approve.

Three suggestion kinds (M2 ships all three):

  reject_keyword       — title n-gram heavily over-represented in rejects
  skip_site            — site whose listings are mostly rejected for this
                          search (e.g. autoscout24 only ever returns cars
                          when you're hunting seats)
  adjust_price_floor   — many "too expensive" rejects clustered above a
                          price threshold → suggest raising max_price_usd

Each kind has a minimum-support gate (default 3 rejects) and a precision
gate (default reject_rate >= 0.75) so noisy single-listing patterns don't
generate junk suggestions.

Read by the API + UI to surface the queue; written from the CLI flag
`--mine-suggestions` (default cron daily ~09:00) or on-demand.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import List, Tuple

from core.database import ListingDB

log = logging.getLogger(__name__)

# Stop-words ignored during tokenisation. Includes generic prose words,
# colour/material words (low information), body-type tokens already
# expressed elsewhere in YAML rejects, and Mk version tokens (also covered
# by the cars-hunt reject list). Tuned to filter year/colour/body-type
# noise that otherwise dominates suggestions from the bulk-rejected
# seat-belt listings.
_STOPWORDS = {
    # Prose / glue
    "the", "a", "an", "of", "for", "with", "and", "or", "to", "in", "by", "from",
    "&", "-", "+", "/", ",", ".", "no", "yes",
    # Stock seller adjectives — noise
    "new", "used", "genuine", "rare", "now", "best", "original", "ready",
    "collect", "collection", "only", "set", "pair", "x", "x2", "x4",
    # Colours / materials — too generic to be a useful reject signal
    "black", "white", "red", "blue", "grey", "gray", "beige", "brown",
    "green", "silver", "gold", "yellow", "orange", "purple", "vinyl",
    "leather", "cloth", "fabric", "plastic", "metal", "chrome",
    # Body / model designators — handled elsewhere or too generic
    "saloon", "estate", "hatchback", "van", "cabriolet", "cabrio",
    "3dr", "4dr", "5dr", "2-door", "4-door", "two-door", "four-door",
    "left", "right", "front", "rear", "passenger", "driver",
    # Mark / variant noise (already in the cars hunt's reject list and
    # not useful as a reject signal for any search hunting Mk1)
    "mk", "mark", "mk1", "mk2", "mk3", "mk4", "mk5", "mk6", "mk7",
    "ford", "escort",
}
# Token regex: words, alphanumeric, allows dashes inside (e.g. "rs-2000")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-]*", re.IGNORECASE)
# Year-or-year-range token (e.g. "1971", "1968-1975") — drop entirely;
# these come from the seat-belt fitment ranges that flood the corpus.
_YEAR_RE = re.compile(r"^(19|20)\d{2}(-(19|20)\d{2})?$")


def _tokenize(title: str | None) -> List[str]:
    if not title:
        return []
    out = []
    for t in _TOKEN_RE.findall(title):
        tl = t.lower()
        if len(tl) < 2 or tl in _STOPWORDS or _YEAR_RE.match(tl):
            continue
        out.append(tl)
    return out


def _ngrams(tokens: List[str], n: int) -> List[str]:
    if n <= 0 or len(tokens) < n:
        return []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def _existing_rejects(db: ListingDB, search_id: int) -> set[str]:
    """Union of YAML reject_title_keywords + DB override reject_keywords.

    Suggestions covering these are no-ops, so skip them.
    """
    import yaml
    out: set[str] = set()
    try:
        with open("config/config.yaml") as f:
            cfg = yaml.safe_load(f)
    except Exception:
        cfg = {}
    # Map search_id back to slug so we can look up its filters in YAML
    slug_row = db.conn.execute(
        "SELECT slug FROM searches WHERE id = ?", (search_id,)
    ).fetchone()
    slug = slug_row["slug"] if slug_row else None
    if slug:
        per_search = (
            cfg.get("searches", {}).get(slug, {}).get("filters", {}) or {}
        )
        for kw in per_search.get("reject_title_keywords") or []:
            out.add(kw.lower())
    # Cars hunt also reads top-level `filters:` block during back-compat
    if slug == "escort_mk1_lhd":
        for kw in (cfg.get("filters", {}).get("reject_title_keywords") or []):
            out.add(kw.lower())
    # DB overrides on top
    override = db.get_search_override(search_id)
    for kw in override["reject_keywords"]:
        out.add(kw.lower())
    return out


def _mine_reject_keywords(
    db: ListingDB,
    search_id: int,
    *,
    min_support: int,
    min_reject_rate: float,
    max_suggestions: int = 20,
) -> int:
    """Find title n-grams over-represented in rejects. Insert as suggestions.
    Returns count of new suggestions inserted (excludes already-known ones).

    Auto-rejected listings (`user_reject_reason` starting "auto:") are
    EXCLUDED from training — they're rule echoes, not user judgement, and
    would poison the corpus with the same titles repeated many times.
    Only manual user-rejects + keeps drive the signal.
    """
    rows = db.conn.execute("""
        SELECT l.url, l.title, l.user_rejected, l.user_reject_reason
        FROM   listings l
        JOIN   search_matches sm ON sm.listing_url = l.url
        WHERE  sm.search_id = ?
          AND  (
                 l.user_rejected = 0
              OR COALESCE(l.user_reject_reason, '') NOT LIKE 'auto:%'
               )
    """, (search_id,)).fetchall()

    reject_counts: Counter[str] = Counter()
    keep_counts: Counter[str] = Counter()
    # Map n-gram → example reject URLs (cap small) for evidence display
    examples: dict[str, list[str]] = {}
    # Map n-gram → most common reject reason for evidence
    reasons_by_ng: dict[str, Counter[str]] = {}

    for r in rows:
        tokens = _tokenize(r["title"])
        # Gather 1-, 2-, 3-grams; the dedup across n keeps each phrase
        # counted only once per listing.
        ngs = set(_ngrams(tokens, 1) + _ngrams(tokens, 2) + _ngrams(tokens, 3))
        is_reject = bool(r["user_rejected"])
        reason = r["user_reject_reason"] or ""
        for ng in ngs:
            if is_reject:
                reject_counts[ng] += 1
                examples.setdefault(ng, [])
                if len(examples[ng]) < 3:
                    examples[ng].append(r["url"])
                reasons_by_ng.setdefault(ng, Counter())[reason] += 1
            else:
                keep_counts[ng] += 1

    existing = _existing_rejects(db, search_id)

    inserted = 0
    for ng, rc in reject_counts.most_common():
        if rc < min_support:
            break  # Counter is sorted desc; below min support won't recover
        if inserted >= max_suggestions:
            break  # Cap per kind so the UI queue isn't overwhelmed.
        kc = keep_counts.get(ng, 0)
        rate = rc / (rc + kc)
        if rate < min_reject_rate:
            continue
        # Skip if already covered (substring match against any existing reject)
        if any(existing_kw in ng or ng in existing_kw for existing_kw in existing):
            continue
        evidence = {
            "reject_count": rc,
            "keep_count": kc,
            "reject_rate": round(rate, 3),
            "top_reasons": dict(reasons_by_ng.get(ng, Counter()).most_common(3)),
            "example_urls": examples.get(ng, []),
        }
        new_id = db.insert_suggestion(search_id, "reject_keyword", ng, evidence)
        if new_id is not None:
            inserted += 1
            log.info("Suggested reject_keyword %r (rejects=%d keeps=%d rate=%.2f) id=%d",
                     ng, rc, kc, rate, new_id)
    return inserted


def _mine_skip_sites(
    db: ListingDB,
    search_id: int,
    *,
    min_support: int,
    min_reject_rate: float,
) -> int:
    """Suggest skipping sites whose listings are overwhelmingly rejected.

    Excludes auto-rejected listings (rule echoes) — same reasoning as
    _mine_reject_keywords. Pure manual-judgement signal only.
    """
    rows = db.conn.execute("""
        SELECT l.site_name,
               SUM(CASE WHEN l.user_rejected = 1
                         AND COALESCE(l.user_reject_reason,'') NOT LIKE 'auto:%'
                        THEN 1 ELSE 0 END) AS rejects,
               SUM(CASE WHEN l.user_rejected = 0 THEN 1 ELSE 0 END) AS keeps,
               SUM(CASE WHEN l.user_rejected = 0
                         OR COALESCE(l.user_reject_reason,'') NOT LIKE 'auto:%'
                        THEN 1 ELSE 0 END) AS total
        FROM   listings l
        JOIN   search_matches sm ON sm.listing_url = l.url
        WHERE  sm.search_id = ?
        GROUP BY l.site_name
    """, (search_id,)).fetchall()

    inserted = 0
    for r in rows:
        site = r["site_name"]
        rc, kc, total = r["rejects"], r["keeps"], r["total"]
        if rc < min_support:
            continue
        rate = rc / total if total else 0
        if rate < min_reject_rate:
            continue
        evidence = {
            "reject_count": rc, "keep_count": kc,
            "reject_rate": round(rate, 3), "total_seen": total,
        }
        new_id = db.insert_suggestion(search_id, "skip_site", site, evidence)
        if new_id is not None:
            inserted += 1
            log.info("Suggested skip_site %r (rejects=%d keeps=%d rate=%.2f) id=%d",
                     site, rc, kc, rate, new_id)
    return inserted


def _mine_price_floor(
    db: ListingDB,
    search_id: int,
    *,
    min_support: int,
) -> int:
    """Suggest raising max_price_usd when many 'too expensive' rejects
    cluster above a threshold. Conservative: only when >= min_support
    'too expensive'-reason rejects exist with a USD value above the
    current cars hunt floor of $35K (placeholder upper bound).
    """
    rows = db.conn.execute("""
        SELECT l.price_value, l.price_currency
        FROM   listings l
        JOIN   search_matches sm ON sm.listing_url = l.url
        WHERE  sm.search_id = ?
          AND  l.user_rejected = 1
          AND  LOWER(COALESCE(l.user_reject_reason, '')) LIKE '%too expensive%'
          AND  l.price_value IS NOT NULL
    """, (search_id,)).fetchall()
    if len(rows) < min_support:
        return 0
    # Bare-bones: USD-convert and pick the cluster's 25th percentile as a
    # candidate ceiling (everything ABOVE this was rejected). Caller can
    # refine before accepting.
    from core.currency import usd_value
    usd_values = sorted(
        v for v in (usd_value(r["price_value"], r["price_currency"]) for r in rows)
        if v is not None
    )
    if len(usd_values) < min_support:
        return 0
    # 25th percentile of the rejected-as-too-expensive cluster
    p25 = usd_values[max(0, len(usd_values) // 4)]
    evidence = {
        "too_expensive_rejects": len(rows),
        "usd_values_sample": [int(v) for v in usd_values[:10]],
        "suggested_ceiling_usd": int(p25),
    }
    new_id = db.insert_suggestion(
        search_id, "adjust_price_ceiling", int(p25), evidence
    )
    return 1 if new_id is not None else 0


def mine_for_search(
    db: ListingDB,
    search_id: int,
    *,
    min_support: int = 3,
    min_reject_rate: float = 0.75,
) -> dict:
    """Mine all three suggestion kinds for a single search.
    Returns counts of newly-inserted suggestions per kind."""
    return {
        "reject_keyword": _mine_reject_keywords(
            db, search_id,
            min_support=min_support, min_reject_rate=min_reject_rate,
        ),
        "skip_site": _mine_skip_sites(
            db, search_id,
            min_support=min_support, min_reject_rate=min_reject_rate,
        ),
        "adjust_price_ceiling": _mine_price_floor(
            db, search_id, min_support=min_support,
        ),
    }


def mine_all_searches(
    db: ListingDB,
    *,
    min_support: int = 3,
    min_reject_rate: float = 0.75,
) -> dict:
    """Mine every search in the DB. Returns {slug: kind-count-dict}."""
    rows = db.conn.execute("SELECT id, slug FROM searches ORDER BY id").fetchall()
    out = {}
    for r in rows:
        log.info("Mining suggestions for search %r (id=%d)", r["slug"], r["id"])
        out[r["slug"]] = mine_for_search(
            db, r["id"],
            min_support=min_support, min_reject_rate=min_reject_rate,
        )
    return out
