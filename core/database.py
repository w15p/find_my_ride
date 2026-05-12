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
    scraped_at      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    sold_at         TEXT
);
"""

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
]


class ListingDB:
    def __init__(self, db_path: str = "listings.db") -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(SCHEMA)
        self._apply_migrations()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_fingerprint ON listings(fingerprint)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_canonical ON listings(canonical_url)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status)")
        self.conn.commit()

    def _apply_migrations(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(listings)")}
        for col, ddl in _MIGRATIONS:
            if col not in existing:
                self.conn.execute(ddl)

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
                description, image_phash, fingerprint, canonical_url, sold_signals_count,
                scraped_at, status, sold_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    l.url, l.site_name, l.title, l.price, l.price_value,
                    l.price_currency, l.year, l.location, l.country_code,
                    l.image_url, l.steering, l.body_type,
                    l.description, l.image_phash, l.fingerprint, l.canonical_url,
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
            """SELECT * FROM listings
               WHERE scraped_at >= ?
                 AND status = 'active'
                 AND canonical_url IS NULL
                 AND user_rejected = 0
               ORDER BY scraped_at DESC""",
            (since.isoformat(),),
        ).fetchall()

    def set_user_reject(self, url: str, reason: Optional[str], rejected: bool = True) -> None:
        if rejected:
            self.conn.execute(
                "UPDATE listings SET user_rejected=1, user_reject_reason=?, user_rejected_at=? WHERE url=?",
                (reason, datetime.utcnow().isoformat(), url),
            )
        else:
            self.conn.execute(
                "UPDATE listings SET user_rejected=0, user_reject_reason=NULL, user_rejected_at=NULL WHERE url=?",
                (url,),
            )
        self.conn.commit()

    def set_user_note(self, url: str, note: Optional[str]) -> None:
        self.conn.execute(
            "UPDATE listings SET user_note=? WHERE url=?",
            (note if (note or "").strip() else None, url),
        )
        self.conn.commit()

    def set_user_field(self, url: str, field: str, value: Optional[str]) -> None:
        """Set a user-override scalar (e.g. user_steering, user_location)."""
        if field not in ("user_steering", "user_location"):
            raise ValueError(f"unsupported user field: {field}")
        v = (value or "").strip() or None
        self.conn.execute(f"UPDATE listings SET {field}=? WHERE url=?", (v, url))
        self.conn.commit()

    def set_user_pin(self, url: str, pinned: bool) -> None:
        if pinned:
            self.conn.execute(
                "UPDATE listings SET user_pinned=1, user_pinned_at=? WHERE url=?",
                (datetime.utcnow().isoformat(), url),
            )
        else:
            self.conn.execute(
                "UPDATE listings SET user_pinned=0, user_pinned_at=NULL WHERE url=?",
                (url,),
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
