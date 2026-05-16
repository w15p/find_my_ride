"""ISO 3166-1 alpha-2 → human-readable country name, plus location enrichment.

Used both at display time (so the UI / digest render "Malta" not "MT") and
inside the FB scraper to recover a country code from FB's free-form
`display_name` string like "Maia, Porto, Portugal".

Coverage: every country relevant to a European classic-car hunt plus the
common buyer-locale countries. Add more as needed.
"""
from __future__ import annotations

import re
from typing import Optional


_ISO_TO_NAME = {
    "AT": "Austria",        "BE": "Belgium",         "BG": "Bulgaria",
    "CH": "Switzerland",    "CY": "Cyprus",          "CZ": "Czechia",
    "DE": "Germany",        "DK": "Denmark",         "EE": "Estonia",
    "ES": "Spain",          "FI": "Finland",         "FR": "France",
    "GB": "United Kingdom", "GR": "Greece",          "HR": "Croatia",
    "HU": "Hungary",        "IE": "Ireland",         "IS": "Iceland",
    "IT": "Italy",          "LT": "Lithuania",       "LU": "Luxembourg",
    "LV": "Latvia",         "MT": "Malta",           "NL": "Netherlands",
    "NO": "Norway",         "PL": "Poland",          "PT": "Portugal",
    "RO": "Romania",        "SE": "Sweden",          "SI": "Slovenia",
    "SK": "Slovakia",       "TR": "Turkey",
    "US": "United States",  "CA": "Canada",          "AU": "Australia",
    "NZ": "New Zealand",    "JP": "Japan",
}

# Reverse map for FB's display_name suffix parsing. Aliases (Spanish names,
# German names, etc.) for the same country are folded in. Lowercase keys
# for case-insensitive matching.
_NAME_TO_ISO = {
    **{name.lower(): iso for iso, name in _ISO_TO_NAME.items()},
    "españa": "ES",         "deutschland": "DE",     "italia": "IT",
    "nederland": "NL",      "belgië": "BE",          "belgique": "BE",
    "uk": "GB",             "england": "GB",         "scotland": "GB",
    "wales": "GB",          "northern ireland": "GB","österreich": "AT",
    "schweiz": "CH",        "suisse": "CH",          "svizzera": "CH",
    "czech republic": "CZ",
}


def country_name(iso: Optional[str]) -> Optional[str]:
    """ISO 3166-1 alpha-2 → English country name. Returns None if unknown."""
    if not iso:
        return None
    return _ISO_TO_NAME.get(iso.upper())


def iso_from_name(name: Optional[str]) -> Optional[str]:
    """Best-effort name → ISO code (case-insensitive, accepts common aliases)."""
    if not name:
        return None
    return _NAME_TO_ISO.get(name.strip().lower())


def country_from_display(display: Optional[str]) -> Optional[str]:
    """Try to recover an ISO country code from a string like 'Maia, Porto, Portugal'."""
    if not display:
        return None
    last_segment = display.rsplit(",", 1)[-1].strip()
    return iso_from_name(last_segment)


def enhance_location(location: Optional[str], country_code: Optional[str] = None) -> Optional[str]:
    """Return a location string with the country name surfaced.

    Rules, in order:
      1. If neither input is set, return None.
      2. If `country_code` is set, expand to country name and:
         - replace the location wholesale when it's the bare ISO (`"MT"` → `"Malta"`)
         - replace a trailing `, ISO` with `, Country` (`"Coimbra, PT"` → `"Coimbra, Portugal"`)
         - append `, Country` only when the country name isn't already in the string
      3. If no `country_code`, scan the location for a bare or trailing ISO
         that maps to a known country and expand it.
    """
    if not location and not country_code:
        return None
    name = country_name(country_code)

    if name:
        if not location:
            return name
        loc = location.strip()
        if name.lower() in loc.lower():
            return loc
        if loc.upper() == (country_code or "").upper():
            return name
        m = re.search(rf",\s*{re.escape((country_code or '').upper())}\s*$", loc)
        if m:
            return loc[: m.start()].rstrip() + f", {name}"
        return f"{loc}, {name}"

    if location:
        loc = location.strip()
        # Bare ISO code like "MT"
        if re.fullmatch(r"[A-Z]{2}", loc):
            expanded = country_name(loc)
            if expanded:
                return expanded
        # Trailing ", XX" where XX is a known ISO
        m = re.search(r",\s*([A-Z]{2})\s*$", loc)
        if m:
            expanded = country_name(m.group(1))
            if expanded:
                return loc[: m.start()].rstrip() + f", {expanded}"
    return location
