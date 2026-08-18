from __future__ import annotations

import random
import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# A realistic pool of browser User-Agents to rotate
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
]


# Longest we will honour a server's Retry-After, in seconds. urllib3 obeys the
# header verbatim by default, so a single host asking for a long wait can park
# an entire scrape inside urllib3 before our own status handling ever sees the
# response. Backing off is right; blocking the run behind one site is not.
# Past the cap the caller gets the response and decides - for eBay that means
# logging the marketplace and moving to the next one.
#
# This is a guard, not a fix for any observed incident: as of 2026-08-18 eBay
# sends no Retry-After with its 429s, so this cap does not currently change
# its behaviour. Measure before blaming a stall on this path.
MAX_RETRY_AFTER = 30.0


class _CappedRetry(Retry):
    """Retry that honours Retry-After but never waits longer than the cap."""

    def get_retry_after(self, response):
        after = super().get_retry_after(response)
        return None if after is None else min(after, MAX_RETRY_AFTER)


def make_session(retries: int = 3, backoff: float = 1.5) -> requests.Session:
    """Create a requests.Session with retry logic and a randomised User-Agent."""
    session = requests.Session()
    retry = _CappedRetry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8,de;q=0.7",
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,*/*;q=0.8"
            ),
        }
    )
    return session


def polite_get(
    session: requests.Session,
    url: str,
    min_delay: float = 2.0,
    max_delay: float = 5.0,
    **kwargs,
) -> requests.Response:
    """GET with a random delay before the request to mimic human pacing."""
    time.sleep(random.uniform(min_delay, max_delay))
    # Rotate User-Agent on each call
    session.headers.update({"User-Agent": random.choice(USER_AGENTS)})
    resp = session.get(url, timeout=30, **kwargs)
    resp.raise_for_status()
    return resp
