"""FastAPI backend for the listings review app.

Single-process, single-DB. Serves the built React app from `webapp/web/dist`
when present, otherwise just exposes `/api/*` for the Vite dev server to proxy.

Endpoints:
  GET   /api/listings        list/filter/sort
  GET   /api/config/reasons  reject-reason dropdown values
  GET   /api/stats           per-site/status/rejected counts
  POST  /api/reject          {url, reason, note?}
  POST  /api/unreject        {url}
  PATCH /api/note            {url, note}

FUTURE: if a parallel hunt is added (e.g. RS2000 seats), `/api/listings` will
need a `category` query param. The endpoint is shaped so adding that param is
a one-line filter, no client breakage.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import sqlite3
import requests
import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlparse

from core import image_cache


class RejectBody(BaseModel):
    url: str
    reason: Optional[str] = None
    note: Optional[str] = None


class UnrejectBody(BaseModel):
    url: str


class NoteBody(BaseModel):
    url: str
    note: Optional[str] = None


class PinBody(BaseModel):
    url: str


class OverrideBody(BaseModel):
    url: str
    steering: Optional[str] = None  # "lhd" | "rhd" | "unknown" | "" (clear)
    location: Optional[str] = None  # free text; "" clears the override
    # Year is accepted as a string so the frontend can send "1971" or "" (clear)
    # without int-coercion gymnastics.
    year: Optional[str] = None
    # ISO 4217 currency override — "EUR" / "GBP" / "USD" / "" (clear).
    # Amount stays as scraped; only the symbol + USD conversion change.
    price_currency: Optional[str] = None
    # Price amount override, in whole units (e.g. "17900" for €17,900).
    # API multiplies by 100 to store cents internally. "" clears.
    price_value: Optional[str] = None


class WatchedAddBody(BaseModel):
    search_id: int
    url: str



from core.countries import enhance_location
from core.currency import format_price, usd_value, SUPPORTED_CURRENCIES
from core.database import ListingDB, _DEFAULT_SEARCH_SLUG, listings_select_sql, user_col_expr

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
WEB_DIST = Path(__file__).resolve().parent / "web" / "dist"

# Image-proxy hosts. Browser can't fetch some of these directly because the
# remote CDN requires a specific Referer header (Car & Classic) or because
# Facebook's image servers reject `localhost` referrers. Proxying lets us set
# the right header server-side. The allowlist also blocks SSRF.
_IMAGE_HOST_RULES = {
    "assets.carandclassic.com": {"referer": "https://www.carandclassic.com/"},
    # Facebook scontent CDN — match any subdomain via suffix below.
    # AS24 and Classic Driver images render without special headers, but
    # going through the proxy uniformly is simpler for the frontend.
    "prod.pictures.autoscout24.net": {"referer": ""},
    "www.classicdriver.com": {"referer": ""},
    "i.ebayimg.com": {"referer": ""},
}
_IMAGE_HOST_SUFFIXES = (
    ".fbcdn.net",          # FB scontent-*.xx.fbcdn.net
    ".carandclassic.com",  # safety net for any C&C subdomain
    ".ebayimg.com",        # safety net for any eBay image subdomain
    ".leparking.fr",       # theparking's own CDN (cloud./img.leparking.fr)
)


def _host_allowed(host: str) -> bool:
    """Static allowlist: the CDNs of our own site scrapers."""
    if host in _IMAGE_HOST_RULES:
        return True
    return any(host.endswith(suf) for suf in _IMAGE_HOST_SUFFIXES)


def _host_is_scraped(db, host: str) -> bool:
    """True if some listing we scraped has its image on `host`.

    theparking is an aggregator whose listings point at the seller's own CDN
    (img.kleinanzeigen.de, apollo.olxcdn.com, media.merrjep.al, ...), and the
    set of source sites is open-ended - a static allowlist would silently
    404 images every time theparking surfaces a new source. Deriving the
    permission from the DB keeps the guard the allowlist exists for (the
    proxy must not become an open SSRF relay into the VPC or the metadata
    service) while self-maintaining: we will fetch a host only when one of
    our own scrapers already stored an image there.
    """
    if not host or len(host) > 253:
        return False
    try:
        row = db.conn.execute(
            "SELECT 1 FROM listings WHERE image_url LIKE 'https://' || ? || '/%' "
            "   OR image_url LIKE 'http://' || ? || '/%' LIMIT 1",
            (host, host),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _referer_for(host: str) -> str:
    rules = _IMAGE_HOST_RULES.get(host)
    if rules:
        return rules.get("referer", "")
    if host.endswith(".fbcdn.net"):
        return ""
    if host.endswith(".carandclassic.com"):
        return "https://www.carandclassic.com/"
    return ""


def _load_cfg() -> dict:
    with open(ROOT / "config" / "config.yaml") as f:
        return yaml.safe_load(f)


def _row_to_dict(r) -> dict:
    d = dict(r)
    # Effective values: user override falls back to scraped value.
    d["display_steering"] = d.get("user_steering") or d.get("steering")
    # User override wins outright; otherwise enrich the scraped location with
    # the country name when it's missing or only the ISO code is present.
    if d.get("user_location"):
        d["display_location"] = d["user_location"]
    else:
        d["display_location"] = enhance_location(d.get("location"), d.get("country_code"))
    d["display_year"] = d.get("user_year") if d.get("user_year") is not None else d.get("year")
    # Currency correction is sticky: a user-corrected currency always wins,
    # even across price re-polls (the scraper mislabels currency consistently).
    d["display_currency"] = d.get("user_price_currency") or d.get("price_currency")
    # User price-AMOUNT override is recency-gated: it wins only while it is
    # at least as recent as the last price re-poll (price_checked_at). Once a
    # newer poll lands, the scraped amount becomes authoritative and the stale
    # manual amount is dropped. A legacy override with no timestamp
    # (user_price_at is None), or a listing never re-polled (price_checked_at
    # is None), keeps honouring the override for backward compatibility.
    user_pv = d.get("user_price_value")
    user_pv_at = d.get("user_price_at")
    price_checked = d.get("price_checked_at")
    override_current = user_pv is not None and (
        user_pv_at is None or price_checked is None or user_pv_at >= price_checked
    )
    effective_pv = user_pv if override_current else d.get("price_value")
    d["display_price_value"] = effective_pv
    # Price-change arrow tracks the SCRAPED price (price_value vs the previous
    # polled value), independent of any manual override. "down" = decrease
    # (good, green), "up" = increase (red). Null when unchanged / no history.
    pv, prev = d.get("price_value"), d.get("prev_price_value")
    if pv is not None and prev is not None and pv != prev:
        d["price_direction"] = "down" if pv < prev else "up"
    else:
        d["price_direction"] = None
    d["prev_display_price"] = (
        format_price(prev, d.get("price_currency")) if prev is not None else None
    )
    # USD conversion uses the *effective* currency so an EUR-corrected listing's
    # $ amount is recomputed at EUR→USD instead of being treated as already USD.
    usd = usd_value(effective_pv, d.get("display_currency"))
    d["price_usd"] = round(usd, 0) if usd is not None else None
    # Always derive the displayed price from the effective price value + effective
    # currency so the symbol is consistent across sites (otherwise eBay
    # leaks its `"GBP 21950.00"` ISO-prefixed string into the UI while
    # every other scraper renders `£21,950`). Fall back to the scraper's
    # raw string only when we don't have structured values.
    d["display_price"] = format_price(effective_pv, d.get("display_currency")) or d.get("price")
    return d


def create_app() -> FastAPI:
    cfg = _load_cfg()
    db_path = cfg.get("database", {}).get("path", "listings.db")

    # Derive the app title from the searches table so it reflects the actual
    # saved-search label rather than a hardcoded string.
    _resolved_db = str(ROOT / db_path) if not Path(db_path).is_absolute() else db_path
    try:
        import sqlite3 as _sqlite3
        _c = _sqlite3.connect(_resolved_db)
        _c.row_factory = _sqlite3.Row
        _row = _c.execute(
            "SELECT label FROM searches WHERE slug = ?", (_DEFAULT_SEARCH_SLUG,)
        ).fetchone()
        _app_title = _row["label"] if _row else "Listings Review"
        _c.close()
    except Exception:
        _app_title = "Listings Review"

    app = FastAPI(title=_app_title)

    # Vite dev server proxies `/api`, but allow direct CORS for flexibility.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(sqlite3.OperationalError)
    def _sqlite_error_handler(_request: Request, exc: sqlite3.OperationalError):
        """Surface DB-busy errors as actionable 503s instead of opaque 500s.

        Caught the user pattern where a cron orphan held the write lock and
        every reject/pin/note returned 'Internal Server Error' with no hint
        what was wrong. With this handler the UI gets a clear, dismissible
        toast naming the cause.
        """
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "Database is busy — likely a long-running cron "
                        "process holding a write lock. Try again in a "
                        "moment, or check `ps -ef | grep run.py` for an "
                        "orphan to kill."
                    ),
                },
            )
        return JSONResponse(status_code=500, content={"detail": f"SQLite error: {exc}"})

    def get_db() -> ListingDB:
        # Per-request connection avoids sqlite cross-thread complaints under
        # uvicorn's threadpool, and keeps cleanup automatic when the handler
        # returns.
        return ListingDB(str(ROOT / db_path) if not Path(db_path).is_absolute() else db_path)

    def _priority_keywords_for(search_id: int) -> list[str]:
        """Lowercased priority_keywords for a search_id, or [] if none.

        Resolves search_id -> slug -> cfg["searches"][slug]["priority_keywords"].
        Priority keywords highlight + float the high-value collector variants;
        they're config-driven and per-search, so a search without them behaves
        exactly as before.
        """
        row = get_db().conn.execute(
            "SELECT slug FROM searches WHERE id=?", (search_id,)
        ).fetchone()
        if not row:
            return []
        sc = (cfg.get("searches") or {}).get(row["slug"]) or {}
        return [k.lower() for k in (sc.get("priority_keywords") or [])]

    # ── Endpoints ────────────────────────────────────────────────────────────

    @app.get("/api/listings")
    def list_listings(
        status: Optional[str] = None,    # None = no filter; api.js omits when "" so this maps to FilterBar's "All status"
        rejected: Optional[int] = 0,        # 0 = hide rejected, 1 = only rejected, -1 = both
        canonical: Optional[int] = 1,       # 1 = canonical only, 0 = include dups, -1 = both
        site: Optional[str] = None,
        q: Optional[str] = None,
        min_usd: Optional[int] = None,
        max_usd: Optional[int] = None,
        year_min: Optional[int] = None,
        year_max: Optional[int] = None,
        steering: Optional[str] = None,
        sort: str = "scraped_at_desc",
        limit: int = 500,
        # search_id scopes results to listings matched under that saved
        # search (joined via search_matches). Default 1 = cars hunt.
        # Existing clients without the param keep seeing the cars feed.
        search_id: int = 1,
    ):
        # `user_*` references in WHERE/ORDER use COALESCE(tls.*, l.*) so that
        # writes to tenant_listing_state override the legacy column. The
        # alias `tls` is bound by listings_select_sql() below.
        u_rejected = user_col_expr("user_rejected")
        u_year     = user_col_expr("user_year")
        u_steering = user_col_expr("user_steering")
        u_pinned   = user_col_expr("user_pinned")
        u_pinnedat = user_col_expr("user_pinned_at")

        clauses = []
        params: list = []
        if status:
            clauses.append("l.status = ?"); params.append(status)
        if rejected == 0:
            clauses.append(f"{u_rejected} = 0")
        elif rejected == 1:
            clauses.append(f"{u_rejected} = 1")
        if canonical == 1:
            clauses.append("l.canonical_url IS NULL")
        elif canonical == 0:
            clauses.append("l.canonical_url IS NOT NULL")
        if site:
            clauses.append("l.site_name = ?"); params.append(site)
        if q:
            clauses.append("(LOWER(l.title) LIKE ? OR LOWER(l.description) LIKE ?)")
            qlike = f"%{q.lower()}%"
            params.extend([qlike, qlike])
        # Filter year and steering by the *effective* value (user override
        # falls back to the scraped value). Otherwise a row whose scraped
        # year=NULL but the user has corrected to 1971 would still be filtered
        # out by `year_min`, which defeats the point of the override.
        if year_min is not None:
            clauses.append(f"(COALESCE({u_year}, l.year) IS NULL OR COALESCE({u_year}, l.year) >= ?)")
            params.append(year_min)
        if year_max is not None:
            clauses.append(f"(COALESCE({u_year}, l.year) IS NULL OR COALESCE({u_year}, l.year) <= ?)")
            params.append(year_max)
        if steering:
            clauses.append(f"(COALESCE({u_steering}, l.steering) = ? OR COALESCE({u_steering}, l.steering) IS NULL)")
            params.append(steering)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        secondary = {
            "scraped_at_desc": "l.scraped_at DESC",
            "scraped_at_asc":  "l.scraped_at ASC",
            "price_asc":       "l.price_value ASC NULLS LAST",
            "price_desc":      "l.price_value DESC NULLS LAST",
            "year_desc":       "l.year DESC NULLS LAST",
        }.get(sort, "l.scraped_at DESC")
        # Pinned rows always float to the top; within the pinned band sort by
        # pin recency (most-recently-pinned first). The user's chosen sort
        # applies to the unpinned band below — and as a tiebreaker inside the
        # pinned band when two cards were pinned at the same instant.
        order = f"{u_pinned} DESC, {u_pinnedat} DESC NULLS LAST, {secondary}"

        db = get_db()
        sql = f"{listings_select_sql(search_id=search_id)} {where} ORDER BY {order} LIMIT ?"
        rows = db.conn.execute(sql, (*params, limit)).fetchall()

        # In-Python USD filter — keeps SQL portable without a Python-side function.
        items = [_row_to_dict(r) for r in rows]
        if min_usd is not None:
            items = [i for i in items if (i["price_usd"] or 0) >= min_usd]
        if max_usd is not None:
            items = [i for i in items if (i["price_usd"] is not None and i["price_usd"] <= max_usd)]

        # Priority highlight: mark items whose title contains one of the
        # search's priority_keywords (the high-value collector variants) and
        # float them to the top. Display-only - never suppresses. Pins still
        # outrank priority; within each band the SQL sort order is preserved
        # (Python's sort is stable).
        prio_kws = _priority_keywords_for(search_id)
        if prio_kws:
            for i in items:
                tl = (i.get("title") or "").lower()
                hit = next((k for k in prio_kws if k in tl), None)
                i["priority"] = bool(hit)
                i["priority_match"] = hit
            items.sort(key=lambda i: (not i.get("user_pinned"), not i.get("priority")))
        else:
            for i in items:
                i["priority"] = False
                i["priority_match"] = None

        # Attach duplicate-source info for canonical rows
        for item in items:
            dups = db.duplicates_of(item["url"]) if item.get("canonical_url") is None else []
            item["also_on"] = [
                {"site_name": d["site_name"], "url": d["url"], "title": d["title"]}
                for d in dups
            ]
        return {"items": items, "count": len(items)}

    @app.get("/api/image")
    def image_proxy(url: str):
        try:
            parsed = urlparse(url)
        except Exception:
            raise HTTPException(400, "bad url")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(400, "bad url")
        if not _host_allowed(parsed.netloc) and not _host_is_scraped(get_db(), parsed.netloc):
            raise HTTPException(403, f"host not allowed: {parsed.netloc}")

        # Cache lookup first — serve from disk if we've ever successfully
        # proxied this URL, even if the origin has since expired the link.
        cached = image_cache.find(url)
        if cached is not None:
            cache_path, media_type = cached
            return FileResponse(
                cache_path,
                media_type=media_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )

        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        ref = _referer_for(parsed.netloc)
        if ref:
            headers["Referer"] = ref
        try:
            r = requests.get(url, headers=headers, timeout=8, stream=True)
        except Exception as exc:
            raise HTTPException(502, f"upstream fetch failed: {exc}")
        if r.status_code != 200:
            raise HTTPException(r.status_code, "upstream returned non-200")

        content_type = (r.headers.get("content-type") or "image/jpeg").split(";")[0].strip().lower()
        # Stream to a temp file in the same shard dir so we can atomically
        # rename into place. If we crash mid-write the temp file is the only
        # casualty — never a half-image cache entry.
        try:
            cache_path = image_cache.write(url, content_type, r.iter_content(8192))
        except Exception as exc:
            log.warning("Image cache write failed for %s: %s — falling back to streaming", url, exc)
            # Fallback: stream straight through without caching. Rare path.
            return StreamingResponse(
                r.iter_content(8192),
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=86400"},
            )

        return FileResponse(
            cache_path,
            media_type=content_type if content_type.startswith("image/") else "application/octet-stream",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/config/reasons")
    def get_reasons():
        cfg = _load_cfg()
        return cfg.get("review", {}).get("reject_reasons", [])

    @app.get("/api/searches")
    def list_searches():
        """List all saved searches for the UI dropdown.

        Returns [{id, slug, label}]. The frontend uses `id` as the
        search_id parameter on /api/listings.
        """
        db = get_db()
        rows = db.conn.execute(
            "SELECT id, slug, label FROM searches ORDER BY id"
        ).fetchall()
        return [{"id": r["id"], "slug": r["slug"], "label": r["label"]} for r in rows]

    @app.get("/api/stats")
    def stats():
        db = get_db()
        u_rejected = user_col_expr("user_rejected")
        rows = db.conn.execute(
            f"""SELECT l.site_name AS site_name,
                       l.status    AS status,
                       {u_rejected} AS user_rejected,
                       COUNT(*) AS n
                FROM listings l
                LEFT JOIN tenant_listing_state tls
                       ON tls.tenant_id = 'default' AND tls.listing_url = l.url
                GROUP BY l.site_name, l.status, {u_rejected}"""
        ).fetchall()
        out = {"active": 0, "sold": 0, "rejected": 0, "by_site": {}}
        for r in rows:
            site_bucket = out["by_site"].setdefault(r["site_name"], {"active": 0, "sold": 0, "rejected": 0})
            if r["user_rejected"]:
                out["rejected"] += r["n"]
                site_bucket["rejected"] += r["n"]
            else:
                key = r["status"] if r["status"] in ("active", "sold") else "other"
                out[key] = out.get(key, 0) + r["n"]
                if key in site_bucket:
                    site_bucket[key] += r["n"]
        return out

    @app.post("/api/reject")
    def reject(body: RejectBody = Body(...)):
        db = get_db()
        existing = db.conn.execute("SELECT url FROM listings WHERE url = ?", (body.url,)).fetchone()
        if not existing:
            raise HTTPException(404, "URL not in DB")
        db.set_user_reject(body.url, body.reason, rejected=True)
        if body.note is not None:
            db.set_user_note(body.url, body.note)
        return {"ok": True}

    @app.post("/api/unreject")
    def unreject(body: UnrejectBody = Body(...)):
        db = get_db()
        db.set_user_reject(body.url, None, rejected=False)
        return {"ok": True}

    @app.post("/api/active")
    def mark_active(body: UnrejectBody = Body(...)):
        """Flip a sold-marked listing back to active. Recovers from
        validate false positives (page errors, FB session-invalid login
        walls) without requiring a shell session against the DB."""
        db = get_db()
        existed = db.conn.execute("SELECT 1 FROM listings WHERE url = ?", (body.url,)).fetchone()
        if not existed:
            raise HTTPException(404, "URL not in DB")
        db.mark_active(body.url)
        return {"ok": True}

    @app.patch("/api/note")
    def update_note(body: NoteBody = Body(...)):
        db = get_db()
        db.set_user_note(body.url, body.note)
        return {"ok": True}

    @app.post("/api/pin")
    def pin(body: PinBody = Body(...)):
        db = get_db()
        existing = db.conn.execute("SELECT url FROM listings WHERE url = ?", (body.url,)).fetchone()
        if not existing:
            raise HTTPException(404, "URL not in DB")
        db.set_user_pin(body.url, pinned=True)
        return {"ok": True}

    @app.post("/api/unpin")
    def unpin(body: PinBody = Body(...)):
        db = get_db()
        db.set_user_pin(body.url, pinned=False)
        return {"ok": True}

    @app.patch("/api/override")
    def update_override(body: OverrideBody = Body(...)):
        db = get_db()
        existing = db.conn.execute("SELECT url FROM listings WHERE url = ?", (body.url,)).fetchone()
        if not existing:
            raise HTTPException(404, "URL not in DB")
        if body.steering is not None:
            allowed = {"", "lhd", "rhd", "unknown"}
            if body.steering.lower() not in allowed:
                raise HTTPException(400, f"steering must be one of {sorted(allowed)}")
            db.set_user_field(body.url, "user_steering", body.steering.lower())
        if body.location is not None:
            db.set_user_field(body.url, "user_location", body.location)
        if body.year is not None:
            raw = body.year.strip()
            if not raw:
                db.set_user_field(body.url, "user_year", None)
            else:
                try:
                    yr = int(raw)
                except ValueError:
                    raise HTTPException(400, f"year must be an integer or empty string, got {body.year!r}")
                if not (1900 <= yr <= 2030):
                    raise HTTPException(400, f"year out of plausible range (1900–2030): {yr}")
                db.set_user_field(body.url, "user_year", yr)
        if body.price_currency is not None:
            allowed = {""} | set(SUPPORTED_CURRENCIES)
            ccy = body.price_currency.strip().upper()
            if ccy not in allowed:
                raise HTTPException(400, f"price_currency must be one of {sorted(allowed)}")
            db.set_user_field(body.url, "user_price_currency", ccy)
        if body.price_value is not None:
            raw = body.price_value.strip()
            if not raw:
                db.set_user_field(body.url, "user_price_value", None)
            else:
                try:
                    whole = int(raw)
                except ValueError:
                    raise HTTPException(400, f"price_value must be a whole number or empty, got {body.price_value!r}")
                if whole < 0:
                    raise HTTPException(400, f"price_value must be non-negative, got {whole}")
                if whole > 100_000_000:
                    # 100M caps the most expensive plausible car at any currency.
                    raise HTTPException(400, f"price_value implausibly large: {whole}")
                # Stored as cents to match the existing price_value column unit.
                db.set_user_field(body.url, "user_price_value", whole * 100)
        return {"ok": True}

    # ── Watched URLs (fetched directly each cron tick, bypass search) ────────

    @app.get("/api/watched")
    def list_watched(search_id: Optional[int] = None):
        """List watched URLs, optionally filtered by search_id.

        Joins with `listings` so the UI can show whether the URL has
        already been pulled in (and its title/price if so).
        """
        db = get_db()
        clauses, params = [], []
        if search_id is not None:
            clauses.append("w.search_id = ?")
            params.append(search_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = db.conn.execute(
            f"""SELECT w.id, w.search_id, w.url, w.added_at,
                       w.last_fetched_at, w.last_status,
                       l.title AS listing_title,
                       l.price AS listing_price,
                       l.image_url AS listing_image_url,
                       l.status AS listing_status
                FROM watched_urls w
                LEFT JOIN listings l ON l.url = w.url
                {where}
                ORDER BY w.id DESC""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]

    @app.post("/api/watched")
    def add_watched(body: WatchedAddBody = Body(...)):
        url = body.url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(400, "url must be http(s)")
        db = get_db()
        exists = db.conn.execute(
            "SELECT 1 FROM searches WHERE id = ?", (body.search_id,)
        ).fetchone()
        if not exists:
            raise HTTPException(404, f"search_id {body.search_id} not found")
        watched_id = db.add_watched_url(body.search_id, url)
        if watched_id is None:
            raise HTTPException(409, "url already watched for this search")
        return {"ok": True, "id": watched_id}

    @app.delete("/api/watched/{watched_id}")
    def delete_watched(watched_id: int):
        db = get_db()
        ok = db.remove_watched_url(watched_id)
        if not ok:
            raise HTTPException(404, f"watched_id {watched_id} not found")
        return {"ok": True}

    # ── Static file serving for the built React app ──────────────────────────

    if WEB_DIST.exists():
        # Serve assets/* from the build, and fall back to index.html for SPA routes.
        app.mount("/assets", StaticFiles(directory=str(WEB_DIST / "assets")), name="assets")

        @app.get("/")
        def root_index():
            return FileResponse(WEB_DIST / "index.html")

        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404)
            target = WEB_DIST / full_path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(WEB_DIST / "index.html")
    else:
        @app.get("/")
        def dev_hint():
            return {
                "message": "Frontend not built. From webapp/web run `npm install && npm run dev` for the Vite dev server, or `npm run build` to produce webapp/web/dist.",
                "api": "/api/listings",
            }

    return app


app = create_app()
