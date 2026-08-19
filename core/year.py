"""Pull a model year out of listing text.

The trap this exists to avoid: a bare four-digit number in a car listing is
not always a year. "Alfa Romeo GTV 2000" and "Ford Escort RS 2000" both name
a model, and reading either as the year 2000 puts a 1970s car in the wrong
decade. That is not cosmetic - `year` feeds the save-time year window, the
dedupe fingerprint, and the duplicate-candidate gate, so one bad value can
both mis-file a listing and stop it matching its own duplicates.

`IGNORED_DESIGNATIONS` therefore lists numbers that are model names in this
project's hunts. Every car tracked here predates 2000, so declining to read
"2000" as a year is always the right call; a hunt that genuinely wanted
year-2000 cars would pass `ignore=frozenset()`.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# Model designations that collide with plausible years.
#   2000 - Alfa 2000 GTV / Berlina / Spider, and Ford Escort RS 2000.
# "RS2000" written solid is already safe (no word boundary before the digits);
# this covers the spaced and standalone spellings.
IGNORED_DESIGNATIONS = frozenset({2000})

# Registration shown as MM/YYYY, which AutoScout24 uses. An explicit date is
# stronger evidence than a loose number, so it is tried first.
_REGISTRATION_RE = re.compile(r"\b\d{1,2}/((?:19|20)\d{2})\b")
_BARE_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def extract_year(
    text: Optional[str],
    *,
    lo: int = 1950,
    hi: int = 2029,
    ignore: Iterable[int] = IGNORED_DESIGNATIONS,
) -> Optional[int]:
    """Return the first plausible model year in `text`, or None.

    A MM/YYYY registration wins over a bare number. Values outside
    [lo, hi] or listed in `ignore` are skipped rather than ending the
    search, so "GTV 2000 (1972)" still yields 1972.
    """
    if not text:
        return None
    ignored = set(ignore)

    for pattern in (_REGISTRATION_RE, _BARE_YEAR_RE):
        for found in pattern.finditer(text):
            value = int(found.group(1))
            if lo <= value <= hi and value not in ignored:
                return value
    return None
