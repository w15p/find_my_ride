from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List, Sequence

import requests

from core.models import Listing

# Default search shape preserves the original Escort Mk1 hunt's behavior.
# When seats / other parallel hunts arrive, run.py passes a different
# `required_keywords` (and eventually a different URL `query`).
DEFAULT_QUERY = "ford escort mk1"
DEFAULT_REQUIRED_KEYWORDS: tuple[str, ...] = ("escort",)


class BaseScraper(ABC):
    site_name: str = ""

    def __init__(
        self,
        config: dict,
        http_client: requests.Session,
        query: str = DEFAULT_QUERY,
        required_keywords: Sequence[str] = DEFAULT_REQUIRED_KEYWORDS,
        extra_params: dict | None = None,
    ) -> None:
        self.config = config
        self.http = http_client
        self.query = query
        # Stored lowercased so per-call comparisons don't re-lower each keyword.
        self.required_keywords = tuple(k.lower() for k in required_keywords)
        # Per-search per-site overrides (e.g. eBay category_ids for the seats
        # hunt). Empty dict by default; each scraper picks out the keys it
        # cares about.
        self.extra_params = extra_params or {}
        self.log = logging.getLogger(self.__class__.__name__)

    def title_matches_search(self, title: str | None) -> bool:
        """True iff `title` contains at least one of `required_keywords`.

        Replaces the per-scraper `if "escort" not in title.lower(): continue`
        guard so a future seats hunt can pass `required_keywords=("seats",
        "rs2000", "mexico")` without each scraper hardcoding "escort".
        """
        if not title:
            return False
        t = title.lower()
        return any(kw in t for kw in self.required_keywords)

    @abstractmethod
    def fetch_listings(self) -> List[Listing]:
        """
        Fetch all current matching listings from the site.

        Must return a list of Listing objects. Must NOT raise — catch all
        site-specific exceptions internally and return [] on unrecoverable
        failure so that a broken site never kills the full run.
        """
        ...

    def _safe_fetch(self) -> List[Listing]:
        """Top-level wrapper; catches any uncaught exception from fetch_listings."""
        try:
            return self.fetch_listings()
        except Exception as exc:
            self.log.error("Fatal error in %s: %s", self.site_name, exc, exc_info=True)
            return []
