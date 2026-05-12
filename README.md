# find_my_ride

A multi-source classic-car listing scraper, deduper, and review app, currently configured to hunt a 2-door LHD Ford Escort Mk1 across UK and Continental European marketplaces.

## Disclaimer

This tool fetches publicly visible listings from third-party marketplaces. Each of those marketplaces has its own terms of service, and several explicitly forbid automated scraping. Running this code is at the operator's discretion and risk; no warranty is provided that any particular use complies with any particular site's terms.

## What it does

- Scrapes six sources every 4 hours via cron: Car & Classic, Classic Driver, eBay, Marktplaats, AutoScout24, Facebook Marketplace.
- Filters at save time against a USD price window, year range, reject-keyword list (Mk2-5, parts, scale models, 4-door, estate, etc.), and steering preference.
- Computes a perceptual hash per listing image (with auto-crop to neutralise blurred CDN padding) so the same car listed on multiple sources collapses to one card.
- Sends a daily HTML email digest at ~08:00 — newest first, with description, price (local + USD), location, drive side, source badge, and an "Also on:" footer for cross-source duplicates.
- Re-checks active listings daily (`--validate`) against a per-site sold-signal list with a two-strike rule so a single false positive can't permanently bury a listing.
- Local FastAPI + React review app for sorting, filtering, pinning favorites, adding free-text notes, manually overriding drive side and location, and rejecting listings with a reason that future digests respect.

## Stack

Python 3.13, SQLite, Playwright (Chromium), FastAPI + Uvicorn, React + Vite + Tailwind, Pillow + imagehash, jinja2, requests, beautifulsoup4.

## First-time setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps          # Linux only; macOS skip
cp config/config.yaml.example config/config.yaml   # if starting fresh; this repo already has one
cp .env.example .env                       # fill in credentials, see "Credentials" below
.venv/bin/python run.py --fb-login          # interactive Facebook login, see "Facebook" below
```

Front-end build (only needed if you edit JSX/CSS):

```bash
cd webapp/web && npm install && npm run build
```

## Credentials (`.env`)

All sensitive values live in `.env` (gitignored). Tracked files contain only env-var names. Required variables:

| Variable | Purpose | Required? |
|---|---|---|
| `SMTP_USER` | Gmail address used to send the digest | yes, for `--send-digest` |
| `SMTP_PASS` | Gmail **app password**, not your account password | yes |
| `DIGEST_RECIPIENTS` | Comma-separated emails to send the digest to | yes |
| `EBAY_APP_ID` | eBay developer App ID (Client ID) | no — see "eBay" |
| `EBAY_CERT_ID` | eBay developer Cert ID (Client Secret) | no — see "eBay" |
| `MARKTPLAATS_CLIENT_ID` | Marktplaats API client ID | no — see "Marktplaats" |
| `MARKTPLAATS_CLIENT_SECRET` | Marktplaats API client secret | no |

### SMTP (Gmail)

Generate a Gmail **app password** at <https://myaccount.google.com/apppasswords> (requires 2FA on the account). Paste it into `.env` as `SMTP_PASS`. Regular account password will not work.

### Facebook Marketplace

Authentication is handled via a persistent Chromium profile at `.fb_profile/` (gitignored). One-time setup:

```bash
.venv/bin/python run.py --fb-login
```

This opens a headed browser. Log in, complete any CAPTCHA / 2FA, and the script will detect the `c_user` cookie and close the browser. The profile persists thereafter; the scrape and validate paths both reuse it.

If the session ever expires, re-run `--fb-login`. Headless re-auth is not possible because of CAPTCHA flows.

### eBay

Requires a free developer account at <https://developer.ebay.com>. **Currently unconfigured** — application submitted but no confirmation received as of writing. Until credentials are in `.env`, the eBay scraper returns 0 rows and logs a 401, but does not crash the run.

Steps once approved:

1. Create an application keyset in the eBay Developer console (Production keys, not sandbox).
2. Copy the App ID (Client ID) and Cert ID (Client Secret) into `.env`.
3. Verify with `.venv/bin/python run.py --sites ebay --check-only`.

### Marktplaats

Optional. Without credentials, the scraper falls back to HTML scraping of `marktplaats.nl/q/ford+escort+mk1/`, which **currently returns 0 results** — the HTML card selectors haven't been re-confirmed against the live site. To use the official API:

1. Register at the Marktplaats developer portal.
2. Put `MARKTPLAATS_CLIENT_ID` and `MARKTPLAATS_CLIENT_SECRET` in `.env`.

### Car & Classic, Classic Driver, AutoScout24

No credentials required. All three scrape public listing pages (Inertia.js JSON / HTML / Playwright respectively). The only knob is the search-page filters defined in `config/config.yaml` under `sites`.

## Running

```bash
# Normal flow (what cron does)
.venv/bin/python run.py                          # scrape -> filter -> dedupe -> save
.venv/bin/python run.py --validate               # sanity-check active rows; mark sold/expired
.venv/bin/python run.py --send-digest            # email last 24h of new active listings
                                                 #   (chains --validate automatically)

# Useful for diagnostics / development
.venv/bin/python run.py --check-only             # scrape and print; don't save or email
.venv/bin/python run.py --sites facebook         # restrict to one or more sites
.venv/bin/python run.py --send-digest --hours 168 # one-week window
.venv/bin/python run.py --send-digest --skip-validate
.venv/bin/python run.py --list-db                # print last 50 stored listings as a table
.venv/bin/python run.py --serve-web              # FastAPI + React review app on :8002
.venv/bin/python run.py --fb-login               # re-auth Facebook (headed browser)
```

### Cron

`cron_run.sh` is a wrapper that sleeps a random offset (`MAX_DRIFT_SECS`, default 2400) before invoking `run.py` with whatever args you pass. Install with `crontab -e`:

```cron
# scrape every 4h, +/-20 min drift
40 */4 * * * /path/to/escort_mk1/cron_run.sh
# daily digest at ~08:00 (chains --validate internally)
40 7 * * * /path/to/escort_mk1/cron_run.sh --send-digest
```

The standalone `--validate` cron entry is no longer required — `--send-digest` runs it first so order is guaranteed regardless of schedule.

## Configuration (`config/config.yaml`)

Most behavior is tunable without code edits:

- `filters.min_price_usd` / `filters.max_price_usd` — anything outside this band is rejected at save time.
- `filters.reject_title_keywords` — single-word tokens match word-bounded; multi-word phrases match as substrings (also normalising hyphens to spaces).
- `filters.phash_max_distance` — Hamming-distance threshold for cross-source dedupe (default 8 after auto-crop).
- `validate.sold_signals` — phrases that mark a listing as sold/expired. Single-word entries match with word boundaries.
- `validate.sold_strike_threshold` — consecutive validate hits required before flipping status (default 2).
- `review.reject_reasons` — drives the reject-reason dropdown in the React app. Read on every API call, no restart needed.
- `sites.<name>.enabled` — turn individual scrapers on/off.
- `sites.facebook.locations` — the 9 search centres + `search_radius_km`.

## Review app

```bash
.venv/bin/python run.py --serve-web
```

Open <http://127.0.0.1:8002>. Features:

- Card grid with hero photo (proxied through `/api/image` so referer-locked CDNs render).
- Sticky filter bar: search-text, site, status, rejected, canonical-only, drive side, USD window, sort order.
- Star-click to pin a card to the top of any view.
- Drive dropdown and free-text location field per card; manual overrides are highlighted blue and revert with a single click.
- Free-text notes that autosave on blur.
- Reject button with a config-driven reason dropdown; rejected listings are hidden by default and excluded from the digest.
- Listings the system thinks are cross-source duplicates collapse into one canonical card with an "Also on:" footer.

The frontend is built into `webapp/web/dist/` and served by FastAPI. For frontend development with HMR, run `cd webapp/web && npm run dev` (proxies `/api` to `:8002`).

## Files of note

```
run.py                  Entry point: scrape, validate, digest, web, CLI
core/
  models.py             Listing dataclass
  database.py           SQLite schema + helpers (idempotent ALTER on init)
  currency.py           USD conversion + cached rate fetch
  notifier.py           HTML/plain email digest rendering
  http_client.py        Shared requests session + polite rate-limit helper
scrapers/
  base.py               BaseScraper interface with error isolation
  carandclassic.py      Inertia.js JSON; handles /l/ and /la/ tiers
  classicdriver.py      BeautifulSoup on search pages
  ebay.py               OAuth + Browse API
  marktplaats.py        OAuth API + JSON-LD + HTML card fallback
  autoscout24.py        Playwright; scroll-then-read for lazy images
  facebook.py           Playwright with persistent profile; DOM + Relay regex
webapp/
  api.py                FastAPI app (listings, reject/unreject, notes, pin/unpin,
                        overrides, image proxy, search)
  web/                  Vite + React + Tailwind frontend
config/config.yaml      Filters, sites, reject reasons, sold signals, etc.
cron_run.sh             Wrapper that adds random drift before invoking run.py
```

## Known issues / status

| Item | Status | Notes |
|---|---|---|
| eBay scraper | Not configured | Awaiting developer credential approval. Returns 0 + logs 401 until `EBAY_APP_ID` / `EBAY_CERT_ID` are set. |
| Marktplaats HTML fallback | Returns 0 | Card selectors haven't been re-confirmed against the live DOM since the most recent redesign. API path is functional if credentials supplied. |
| AutoScout24 IP geo | Watch | AS24 may serve different content from US datacenter IPs vs European IPs. If deploying to AWS, prefer `eu-west-1` or `eu-central-1`. |
| Facebook session expiry | Manual | When the FB cookie expires, scrape silently returns 0. Re-run `--fb-login`. |
| Single-tenant | Intentional | Pin/note/reject state is shared by all users of a deployment. Multi-tenancy would require a per-user state table and per-user search definitions (not currently planned). |

## Architecture quick-take

Listings flow `scraper -> _should_keep filter -> URL-dedupe -> phash + fingerprint compute -> cross-source dedupe -> save`. Filter rules and reject keywords are central (`run.py`) so individual scrapers stay dumb. The database is a single SQLite file with idempotent column-add migrations — schema changes don't require manual intervention.

The review app reads the same DB the cron does. User state (pin / note / reject / drive override / location override / sold-strike counter) lives on the `listings` row itself; the digest query is `WHERE status = 'active' AND canonical_url IS NULL AND user_rejected = 0`.
