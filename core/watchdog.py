"""SIGALRM-based watchdog for bounding long-running operations.

Designed for the recurring cron-orphan failure mode: a Playwright
`page.goto` or HTTP retry loop hangs indefinitely, the cron process keeps
its DB connection open, the webapp can't acquire a write lock, and every
user action (reject/pin/note) returns 'database is locked' until someone
SSHs in and kills the orphan by hand.

With this watchdog wrapped around each phase (per-site scrape, validate
loop, digest send), a hung operation fires SIGALRM at the configured
budget, raises `OperationTimeout`, the caller catches and continues with
the next phase. Worst case the whole process exits — but cleanly, and the
next cron tick reruns the missed work.

Unix-only (signal.alarm). Macros to a no-op on Windows so tests pass.
"""
from __future__ import annotations

import logging
import signal
from contextlib import contextmanager
from typing import Iterator


class OperationTimeout(Exception):
    """Raised when a watchdog timer fires before its `with` block exits."""


@contextmanager
def watchdog(timeout_seconds: int, operation: str) -> Iterator[None]:
    """Bound `operation` to `timeout_seconds`. Raise `OperationTimeout` on fire.

    Usage:
        try:
            with watchdog(30 * 60, "scrape-facebook"):
                scraper.fetch_listings()
        except OperationTimeout:
            log.warning("FB scrape exceeded budget — moving on")

    Signals to know:
    - Nested watchdogs are not supported (inner alarm replaces outer).
      If you need nested budgets, use the outermost only.
    - SIGALRM is delivered to the main thread. Background threads keep
      running until the main thread exits.
    - The signal interrupts most blocking syscalls (read/write/select),
      so Playwright's pipe IO and `requests`' socket reads are reachable.
      A spin loop in pure C (rare) would only be interrupted at the next
      Python bytecode boundary.
    """
    log = logging.getLogger("watchdog")

    if not hasattr(signal, "SIGALRM"):
        # Windows / unusual platforms — no-op. The hang risk is real on
        # cron-driven Linux/macOS; if you're running this on Windows
        # interactively you'll Ctrl-C out of hangs yourself.
        yield
        return

    def _handler(_signum, _frame):
        log.warning(
            "Watchdog tripped: %r exceeded %ds budget — aborting.",
            operation, timeout_seconds,
        )
        raise OperationTimeout(
            f"{operation} exceeded {timeout_seconds}s budget"
        )

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout_seconds)
    try:
        yield
    finally:
        signal.alarm(0)  # cancel any pending alarm before restoring handler
        signal.signal(signal.SIGALRM, old_handler)
