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

import requests
import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from urllib.parse import urlparse


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



from core.countries import enhance_location
from core.currency import format_price, usd_value
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
)


def _host_allowed(host: str) -> bool:
    if host in _IMAGE_HOST_RULES:
        return True
    return any(host.endswith(suf) for suf in _IMAGE_HOST_SUFFIXES)


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
    d["display_currency"] = d.get("user_price_currency") or d.get("price_currency")
    # USD conversion uses the *effective* currency so an EUR-corrected listing's
    # $ amount is recomputed at EUR→USD instead of being treated as already USD.
    usd = usd_value(d.get("price_value"), d.get("display_currency"))
    d["price_usd"] = round(usd, 0) if usd is not None else None
    # Always derive the displayed price from `price_value` + effective
    # currency so the symbol is consistent across sites (otherwise eBay
    # leaks its `"GBP 21950.00"` ISO-prefixed string into the UI while
    # every other scraper renders `£21,950`). Fall back to the scraper's
    # raw string only when we don't have structured values.
    d["display_price"] = format_price(d.get("price_value"), d.get("display_currency")) or d.get("price")
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

    def get_db() -> ListingDB:
        # Per-request connection avoids sqlite cross-thread complaints under
        # uvicorn's threadpool, and keeps cleanup automatic when the handler
        # returns.
        return ListingDB(str(ROOT / db_path) if not Path(db_path).is_absolute() else db_path)

    # ── Endpoints ────────────────────────────────────────────────────────────

    @app.get("/api/listings")
    def list_listings(
        status: Optional[str] = "active",
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
        sql = f"{listings_select_sql()} {where} ORDER BY {order} LIMIT ?"
        rows = db.conn.execute(sql, (*params, limit)).fetchall()

        # In-Python USD filter — keeps SQL portable without a Python-side function.
        items = [_row_to_dict(r) for r in rows]
        if min_usd is not None:
            items = [i for i in items if (i["price_usd"] or 0) >= min_usd]
        if max_usd is not None:
            items = [i for i in items if (i["price_usd"] is not None and i["price_usd"] <= max_usd)]

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
        if not _host_allowed(parsed.netloc):
            raise HTTPException(403, f"host not allowed: {parsed.netloc}")
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
        content_type = r.headers.get("content-type", "image/jpeg")
        return StreamingResponse(
            r.iter_content(8192),
            media_type=content_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/config/reasons")
    def get_reasons():
        cfg = _load_cfg()
        return cfg.get("review", {}).get("reject_reasons", [])

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
            allowed = {"", "EUR", "GBP", "USD"}
            ccy = body.price_currency.strip().upper()
            if ccy not in allowed:
                raise HTTPException(400, f"price_currency must be one of {sorted(allowed)}")
            db.set_user_field(body.url, "user_price_currency", ccy)
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
