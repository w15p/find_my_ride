from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import List, Optional

from core.models import Listing

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    url             TEXT PRIMARY KEY,
    site_name       TEXT NOT NULL,
    title           TEXT,
    price           TEXT,
    price_value     INTEGER,
    price_currency  TEXT,
    year            INTEGER,
    location        TEXT,
    country_code    TEXT,
    image_url       TEXT,
    steering        TEXT,
    body_type       TEXT,
    description         TEXT,
    image_phash         TEXT,
    fingerprint         TEXT,
    canonical_url       TEXT,
    sold_signals_count  INTEGER NOT NULL DEFAULT 0,
    user_rejected       INTEGER NOT NULL DEFAULT 0,
    user_reject_reason  TEXT,
    user_rejected_at    TEXT,
    user_note           TEXT,
    user_pinned         INTEGER NOT NULL DEFAULT 0,
    user_pinned_at      TEXT,
    user_steering       TEXT,
    user_location       TEXT,
    user_year           INTEGER,
    user_price_currency TEXT,
    description_language    TEXT,
    description_translated  TEXT,
    scraped_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    sold_at         TEXT
);
"""

SCHEMA_SEARCHES = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    id          TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_matches (
    search_id   INTEGER NOT NULL REFERENCES searches(id),
    listing_url TEXT    NOT NULL REFERENCES listings(url),
    matched_at  TEXT    NOT NULL,
    PRIMARY KEY (search_id, listing_url)
);

CREATE TABLE IF NOT EXISTS tenant_listing_state (
    tenant_id           TEXT    NOT NULL,
    listing_url         TEXT    NOT NULL REFERENCES listings(url),
    user_rejected       INTEGER NOT NULL DEFAULT 0,
    user_reject_reason  TEXT,
    user_rejected_at    TEXT,
    user_note           TEXT,
    user_pinned         INTEGER NOT NULL DEFAULT 0,
    user_pinned_at      TEXT,
    user_steering       TEXT,
    user_location       TEXT,
    user_year           INTEGER,
    user_price_currency TEXT,
    PRIMARY KEY (tenant_id, listing_url)
);
"""

_STRUCTURAL_MIGRATION_ID = "001_search_split"
_SEATS_SEARCH_MIGRATION_ID = "002_seats_search"
_DEFAULT_SEARCH_SLUG = "escort_mk1_lhd"
_DEFAULT_SEARCH_LABEL = "Ford Escort Mk1 LHD"
_DEFAULT_TENANT_ID = "default"

# Second saved search — RS2000 / Mexico seats hunt. Slug used by run.py and the
# webapp to address this search; label is what the UI/email shows. Per-search
# config (query, required_keywords, sites, filters) lives in config/config.yaml.
_SEATS_SEARCH_SLUG = "rs2000_mexico_seats"
_SEATS_SEARCH_LABEL = "RS2000 / Mexico Seats"

# Columns on `listings` that are NOT user-state. Used by listings_select_sql()
# below to build a deduplicated SELECT list when joining tenant_listing_state.
_LISTING_COLS_NON_USER = (
    "url", "site_name", "title", "price", "price_value", "price_currency",
    "year", "location", "country_code", "image_url", "steering", "body_type",
    "description", "image_phash", "fingerprint", "canonical_url",
    "sold_signals_count", "scraped_at", "status", "sold_at",
    "description_language", "description_translated",
)
# Per-tenant overrides — kept on both `listings` (legacy, write-through) and
# `tenant_listing_state` (forward target). Reads COALESCE tls over legacy.
_USER_STATE_COLS = (
    "user_rejected", "user_reject_reason", "user_rejected_at",
    "user_note", "user_pinned", "user_pinned_at",
    "user_steering", "user_location", "user_year", "user_price_currency",
)


import re as _re
_SAFE_TENANT_ID = _re.compile(r"^[A-Za-z0-9_\-]+$")


def _validate_tenant_id(tenant_id: str) -> str:
    # tenant_id is interpolated into SQL string fragments below; restrict to
    # a safe character set so a future dynamic caller can't break out.
    if not _SAFE_TENANT_ID.match(tenant_id or ""):
        raise ValueError(f"unsafe tenant_id: {tenant_id!r}")
    return tenant_id


def listings_select_sql(tenant_id: str = _DEFAULT_TENANT_ID) -> str:
    """Return `SELECT … FROM listings l LEFT JOIN tenant_listing_state tls …`
    suitable for use as the prefix of any listing query. User-state columns
    appear once, COALESCE-aliased to their legacy names, so callers (and
    `_row_to_dict` / `_row_to_listing`) see the effective value transparently.
    """
    tenant_id = _validate_tenant_id(tenant_id)
    non_user = ", ".join(f"l.{c}" for c in _LISTING_COLS_NON_USER)
    user = ", ".join(
        f"COALESCE(tls.{c}, l.{c}) AS {c}" for c in _USER_STATE_COLS
    )
    return (
        f"SELECT {non_user}, {user} "
        f"FROM listings l "
        f"LEFT JOIN tenant_listing_state tls "
        f"ON tls.tenant_id = '{tenant_id}' AND tls.listing_url = l.url"
    )


def user_col_expr(col: str) -> str:
    """Inline COALESCE expression for a user_* column, for WHERE/ORDER clauses.
    Assumes the surrounding query aliases `listings` AS `l` and joins
    `tenant_listing_state` AS `tls`.
    """
    if col not in _USER_STATE_COLS:
        raise ValueError(f"not a user-state column: {col}")
    return f"COALESCE(tls.{col}, l.{col})"

_MIGRATIONS = [
    ("description",        "ALTER TABLE listings ADD COLUMN description TEXT"),
    ("image_phash",        "ALTER TABLE listings ADD COLUMN image_phash TEXT"),
    ("fingerprint",        "ALTER TABLE listings ADD COLUMN fingerprint TEXT"),
    ("canonical_url",      "ALTER TABLE listings ADD COLUMN canonical_url TEXT"),
    ("sold_signals_count", "ALTER TABLE listings ADD COLUMN sold_signals_count INTEGER NOT NULL DEFAULT 0"),
    ("user_rejected",      "ALTER TABLE listings ADD COLUMN user_rejected INTEGER NOT NULL DEFAULT 0"),
    ("user_reject_reason", "ALTER TABLE listings ADD COLUMN user_reject_reason TEXT"),
    ("user_rejected_at",   "ALTER TABLE listings ADD COLUMN user_rejected_at TEXT"),
    ("user_note",          "ALTER TABLE listings ADD COLUMN user_note TEXT"),
    ("user_pinned",        "ALTER TABLE listings ADD COLUMN user_pinned INTEGER NOT NULL DEFAULT 0"),
    ("user_pinned_at",     "ALTER TABLE listings ADD COLUMN user_pinned_at TEXT"),
    ("user_steering",      "ALTER TABLE listings ADD COLUMN user_steering TEXT"),
    ("user_location",      "ALTER TABLE listings ADD COLUMN user_location TEXT"),
    ("user_year",          "ALTER TABLE listings ADD COLUMN user_year INTEGER"),
    ("user_price_currency","ALTER TABLE listings ADD COLUMN user_price_currency TEXT"),
    ("description_language",   "ALTER TABLE listings ADD COLUMN description_language TEXT"),
    ("description_translated", "ALTER TABLE listings ADD COLUMN description_translated TEXT"),
]


class ListingDB:
    def __init__(self, db_path: str = "listings.db") -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL lets the CLI scraper and the FastAPI process share the file
        # without writer-lock contention. Per-connection PRAGMA but applies
        # file-wide once set, so it's harmless on every open.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.executescript(SCHEMA_SEARCHES)
        self._apply_migrations()
        self._apply_structural_migrations()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_canonical ON listings(canonical_url)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_search_matches_listing ON search_matches(listing_url)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_tls_listing ON tenant_listing_state(listing_url)")
        self.conn.commit()

    def _apply_migrations(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(listings)")}
        for col, ddl in _MIGRATIONS:
            if col not in existing:
                self.conn.execute(ddl)

    def _apply_structural_migrations(self) -> None:
        """One-shot data migrations gated by `schema_migrations`.

        Idempotent: a `SELECT` on the gate table is the fast path after the
        first run. Wrapped in an explicit transaction so a partial failure
        leaves the legacy state authoritative and the new tables empty —
        the next startup retries.
        """
        already = self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?",
            (_STRUCTURAL_MIGRATION_ID,),
        ).fetchone()
        if already:
            # 001 already done — fall through to later migrations rather than
            # short-circuiting. Each subsequent migration owns its own gate.
            self._migrate_seats_search()
            return

        now = datetime.utcnow().isoformat()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO searches (slug, label, created_at) VALUES (?, ?, ?)",
                (_DEFAULT_SEARCH_SLUG, _DEFAULT_SEARCH_LABEL, now),
            )
            self.conn.execute(
                """INSERT OR IGNORE INTO search_matches (search_id, listing_url, matched_at)
                   SELECT s.id, l.url, ?
                   FROM   listings l
                   JOIN   searches s ON s.slug = ?""",
                (now, _DEFAULT_SEARCH_SLUG),
            )
            # Skip listings with no non-default user state — those rows would
            # be no-op carriers and only inflate the table. Writers upsert,
            # so absence of a row is correctly interpreted as "all defaults."
            self.conn.execute(
                """INSERT OR IGNORE INTO tenant_listing_state (
                       tenant_id, listing_url,
                       user_rejected, user_reject_reason, user_rejected_at,
                       user_note,
                       user_pinned, user_pinned_at,
                       user_steering, user_location, user_year, user_price_currency
                   )
                   SELECT
                       ?, url,
                       user_rejected, user_reject_reason, user_rejected_at,
                       user_note,
                       user_pinned, user_pinned_at,
                       user_steering, user_location, user_year, user_price_currency
                   FROM listings
                   WHERE user_rejected = 1
                      OR user_pinned   = 1
                      OR user_note     IS NOT NULL
                      OR user_steering IS NOT NULL
                      OR user_location IS NOT NULL
                      OR user_year     IS NOT NULL
                      OR user_price_currency IS NOT NULL""",
                (_DEFAULT_TENANT_ID,),
            )
            self.conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (_STRUCTURAL_MIGRATION_ID, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

        self._migrate_seats_search()

    def _migrate_seats_search(self) -> None:
        """Seed the `searches` row for the RS2000 / Mexico seats hunt.

        Idempotent and gated on `schema_migrations`. Independent of the
        `001_search_split` gate so that even on a DB where 001 already ran
        (every existing install), this second seed still gets applied on
        next startup.
        """
        already = self.conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?",
            (_SEATS_SEARCH_MIGRATION_ID,),
        ).fetchone()
        if already:
            return

        now = datetime.utcnow().isoformat()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO searches (slug, label, created_at) VALUES (?, ?, ?)",
                (_SEATS_SEARCH_SLUG, _SEATS_SEARCH_LABEL, now),
            )
            self.conn.execute(
                "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (_SEATS_SEARCH_MIGRATION_ID, now),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def filter_new(self, listings: List[Listing]) -> List[Listing]:
        """Return only listings whose URL is not already stored."""
        if not listings:
            return []
        seen = {row["url"] for row in self.conn.execute("SELECT url FROM listings")}
        return [l for l in listings if l.url not in seen]

    def save(self, listings: List[Listing]) -> None:
        """Insert new listings; silently skip duplicates by URL."""
        self.conn.executemany(
            """INSERT OR IGNORE INTO listings
               (url, site_name, title, price, price_value, price_currency,
                year, location, country_code, image_url, steering, body_type,
                description, description_language, description_translated,
                image_phash, fingerprint, canonical_url, sold_signals_count,
                scraped_at, status, sold_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    l.url, l.site_name, l.title, l.price, l.price_value,
                    l.price_currency, l.year, l.location, l.country_code,
                    l.image_url, l.steering, l.body_type,
                    l.description, l.description_language, l.description_translated,
                    l.image_phash, l.fingerprint, l.canonical_url,
                    l.sold_signals_count,
                    l.scraped_at.isoformat(), l.status,
                    l.sold_at.isoformat() if l.sold_at else None,
                )
                for l in listings
            ],
        )
        self.conn.commit()

    def mark_status(self, url: str, status: str) -> None:
        """Update the status of a stored listing (e.g. sold/expired)."""
        sold_at = datetime.utcnow().isoformat() if status in ("sold", "expired") else None
        self.conn.execute(
            "UPDATE listings SET status=?, sold_at=? WHERE url=?",
            (status, sold_at, url),
        )
        self.conn.commit()

    def increment_sold_signal(self, url: str) -> int:
        """Bump the sold-signal counter; return the new value."""
        cur = self.conn.execute(
            "UPDATE listings SET sold_signals_count = sold_signals_count + 1 WHERE url=? RETURNING sold_signals_count",
            (url,),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row[0] if row else 0

    def reset_sold_signal(self, url: str) -> None:
        """Reset the counter when a listing is observed as still active."""
        self.conn.execute(
            "UPDATE listings SET sold_signals_count = 0 WHERE url=? AND sold_signals_count > 0",
            (url,),
        )

    def update_image_url(self, url: str, image_url: Optional[str]) -> None:
        """Refresh the stored image URL for a listing.

        Facebook's CDN signs image URLs with a ~24-48h TTL (`oe=` query
        param), so stored URLs go stale and start returning 403 after a day
        or so. The `--refresh-fb-images` CLI command walks active FB
        listings and calls this to swap in a fresh URL.
        """
        self.conn.execute(
            "UPDATE listings SET image_url=? WHERE url=?",
            (image_url, url),
        )
        self.conn.commit()
        self.conn.commit()

    def update_dedupe_fields(self, url: str, *, image_phash: Optional[str], fingerprint: Optional[str], canonical_url: Optional[str]) -> None:
        """Backfill phash/fingerprint/canonical on an existing row."""
        self.conn.execute(
            "UPDATE listings SET image_phash=?, fingerprint=?, canonical_url=? WHERE url=?",
            (image_phash, fingerprint, canonical_url, url),
        )
        self.conn.commit()

    def active_listings(self) -> List[sqlite3.Row]:
        """Return all listings with status='active'."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE status='active' ORDER BY scraped_at DESC"
        ).fetchall()

    def recent_listings(self, limit: int = 50) -> List[sqlite3.Row]:
        """Return the most recently scraped listings regardless of status."""
        return self.conn.execute(
            "SELECT * FROM listings ORDER BY scraped_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def listings_since(self, since: datetime) -> List[sqlite3.Row]:
        """Return listings scraped after `since` (UTC), newest first."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE scraped_at >= ? ORDER BY scraped_at DESC",
            (since.isoformat(),),
        ).fetchall()

    def canonical_listings_since(self, since: datetime) -> List[sqlite3.Row]:
        """Return canonical (non-duplicate), active, not-user-rejected listings."""
        return self.conn.execute(
            f"""{listings_select_sql()}
                WHERE l.scraped_at >= ?
                  AND l.status = 'active'
                  AND l.canonical_url IS NULL
                  AND {user_col_expr('user_rejected')} = 0
                ORDER BY l.scraped_at DESC""",
            (since.isoformat(),),
        ).fetchall()

    def set_user_reject(self, url: str, reason: Optional[str], rejected: bool = True) -> None:
        # Shadow-write: legacy column UPDATE stays authoritative for reads;
        # tenant_listing_state upsert is the forward-compatible target. Upsert
        # (not UPDATE) because the structural backfill only created tls rows
        # for listings with non-default state — a UPDATE would silently miss.
        if rejected:
            ts = datetime.utcnow().isoformat()
            self.conn.execute(
                "UPDATE listings SET user_rejected=1, user_reject_reason=?, user_rejected_at=? WHERE url=?",
                (reason, ts, url),
            )
            self.conn.execute(
                """INSERT INTO tenant_listing_state
                       (tenant_id, listing_url, user_rejected, user_reject_reason, user_rejected_at)
                   VALUES (?, ?, 1, ?, ?)
                   ON CONFLICT (tenant_id, listing_url) DO UPDATE SET
                       user_rejected      = 1,
                       user_reject_reason = excluded.user_reject_reason,
                       user_rejected_at   = excluded.user_rejected_at""",
                (_DEFAULT_TENANT_ID, url, reason, ts),
            )
        else:
            self.conn.execute(
                "UPDATE listings SET user_rejected=0, user_reject_reason=NULL, user_rejected_at=NULL WHERE url=?",
                (url,),
            )
            self.conn.execute(
                """INSERT INTO tenant_listing_state
                       (tenant_id, listing_url, user_rejected)
                   VALUES (?, ?, 0)
                   ON CONFLICT (tenant_id, listing_url) DO UPDATE SET
                       user_rejected      = 0,
                       user_reject_reason = NULL,
                       user_rejected_at   = NULL""",
                (_DEFAULT_TENANT_ID, url),
            )
        self.conn.commit()

    def set_user_note(self, url: str, note: Optional[str]) -> None:
        value = note if (note or "").strip() else None
        self.conn.execute(
            "UPDATE listings SET user_note=? WHERE url=?",
            (value, url),
        )
        self.conn.execute(
            """INSERT INTO tenant_listing_state (tenant_id, listing_url, user_note)
               VALUES (?, ?, ?)
               ON CONFLICT (tenant_id, listing_url) DO UPDATE SET user_note = excluded.user_note""",
            (_DEFAULT_TENANT_ID, url, value),
        )
        self.conn.commit()

    def set_user_field(self, url: str, field: str, value) -> None:
        """Set a user-override scalar (user_steering, user_location, user_year).

        `value` may be a string (text fields), an int (year), or None / empty
        string / whitespace to clear the override.
        """
        if field not in ("user_steering", "user_location", "user_year", "user_price_currency"):
            raise ValueError(f"unsupported user field: {field}")
        if value is None or (isinstance(value, str) and not value.strip()):
            v = None
        elif field == "user_year":
            v = int(value)
        else:
            v = str(value).strip()
        self.conn.execute(f"UPDATE listings SET {field}=? WHERE url=?", (v, url))
        # `field` is already allowlist-validated above, so the f-string is safe.
        self.conn.execute(
            f"""INSERT INTO tenant_listing_state (tenant_id, listing_url, {field})
                VALUES (?, ?, ?)
                ON CONFLICT (tenant_id, listing_url) DO UPDATE SET {field} = excluded.{field}""",
            (_DEFAULT_TENANT_ID, url, v),
        )
        self.conn.commit()

    def set_user_pin(self, url: str, pinned: bool) -> None:
        if pinned:
            ts = datetime.utcnow().isoformat()
            self.conn.execute(
                "UPDATE listings SET user_pinned=1, user_pinned_at=? WHERE url=?",
                (ts, url),
            )
            self.conn.execute(
                """INSERT INTO tenant_listing_state
                       (tenant_id, listing_url, user_pinned, user_pinned_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT (tenant_id, listing_url) DO UPDATE SET
                       user_pinned    = 1,
                       user_pinned_at = excluded.user_pinned_at""",
                (_DEFAULT_TENANT_ID, url, ts),
            )
        else:
            self.conn.execute(
                "UPDATE listings SET user_pinned=0, user_pinned_at=NULL WHERE url=?",
                (url,),
            )
            self.conn.execute(
                """INSERT INTO tenant_listing_state
                       (tenant_id, listing_url, user_pinned)
                   VALUES (?, ?, 0)
                   ON CONFLICT (tenant_id, listing_url) DO UPDATE SET
                       user_pinned    = 0,
                       user_pinned_at = NULL""",
                (_DEFAULT_TENANT_ID, url),
            )
        self.conn.commit()

    def duplicates_of(self, canonical_url: str) -> List[sqlite3.Row]:
        """Return all listings that point at canonical_url."""
        return self.conn.execute(
            "SELECT * FROM listings WHERE canonical_url = ? ORDER BY scraped_at ASC",
            (canonical_url,),
        ).fetchall()

    def find_duplicate_candidates(self, *, year: Optional[int], country_code: Optional[str]) -> List[sqlite3.Row]:
        """
        Return existing canonical listings that could plausibly match a candidate
        (year + country gate). Returned rows include url, fingerprint, image_phash,
        price_value, price_currency for the caller to score.
        """
        clauses = ["canonical_url IS NULL"]
        params: list = []
        if year is None:
            clauses.append("year IS NULL")
        else:
            clauses.append("(year = ? OR year IS NULL)")
            params.append(year)
        if country_code is None:
            pass
        else:
            clauses.append("(country_code = ? OR country_code IS NULL)")
            params.append(country_code)
        sql = (
            "SELECT url, fingerprint, image_phash, price_value, price_currency, year "
            f"FROM listings WHERE {' AND '.join(clauses)}"
        )
        return self.conn.execute(sql, tuple(params)).fetchall()
