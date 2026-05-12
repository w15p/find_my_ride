from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import List

import requests

from core.models import Listing


class BaseScraper(ABC):
    site_name: str = ""

    def __init__(self, config: dict, http_client: requests.Session) -> None:
        self.config = config
        self.http = http_client
        self.log = logging.getLogger(self.__class__.__name__)

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
