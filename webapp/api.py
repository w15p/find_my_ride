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

from core.currency import usd_value
from core.database import ListingDB

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
}
_IMAGE_HOST_SUFFIXES = (
    ".fbcdn.net",          # FB scontent-*.xx.fbcdn.net
    ".carandclassic.com",  # safety net for any C&C subdomain
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
    usd = usd_value(d.get("price_value"), d.get("price_currency"))
    d["price_usd"] = round(usd, 0) if usd is not None else None
    # Effective display values fall back to scraped if the user hasn't overridden.
    # Frontend renders these; the original columns stay visible for "is this
    # a manual override?" indication on the UI.
    d["display_steering"] = d.get("user_steering") or d.get("steering")
    d["display_location"] = d.get("user_location") or d.get("location")
    return d


def create_app() -> FastAPI:
    cfg = _load_cfg()
    db_path = cfg.get("database", {}).get("path", "listings.db")
    app = FastAPI(title="Escort Mk1 Review")

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
        clauses = []
        params: list = []
        if status:
            clauses.append("status = ?"); params.append(status)
        if rejected == 0:
            clauses.append("user_rejected = 0")
        elif rejected == 1:
            clauses.append("user_rejected = 1")
        if canonical == 1:
            clauses.append("canonical_url IS NULL")
        elif canonical == 0:
            clauses.append("canonical_url IS NOT NULL")
        if site:
            clauses.append("site_name = ?"); params.append(site)
        if q:
            clauses.append("(LOWER(title) LIKE ? OR LOWER(description) LIKE ?)")
            qlike = f"%{q.lower()}%"
            params.extend([qlike, qlike])
        if year_min is not None:
            clauses.append("(year IS NULL OR year >= ?)"); params.append(year_min)
        if year_max is not None:
            clauses.append("(year IS NULL OR year <= ?)"); params.append(year_max)
        if steering:
            clauses.append("(steering = ? OR steering IS NULL)"); params.append(steering)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        secondary = {
            "scraped_at_desc": "scraped_at DESC",
            "scraped_at_asc":  "scraped_at ASC",
            "price_asc":       "price_value ASC NULLS LAST",
            "price_desc":      "price_value DESC NULLS LAST",
            "year_desc":       "year DESC NULLS LAST",
        }.get(sort, "scraped_at DESC")
        # Pinned rows always float to the top; within the pinned band sort by
        # pin recency (most-recently-pinned first). The user's chosen sort
        # applies to the unpinned band below — and as a tiebreaker inside the
        # pinned band when two cards were pinned at the same instant.
        order = f"user_pinned DESC, user_pinned_at DESC NULLS LAST, {secondary}"

        db = get_db()
        sql = f"SELECT * FROM listings {where} ORDER BY {order} LIMIT ?"
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
        rows = db.conn.execute(
            "SELECT site_name, status, user_rejected, COUNT(*) AS n FROM listings GROUP BY site_name, status, user_rejected"
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
