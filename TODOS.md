# Open Threads

In-flight items as of 2026-05-18. Update as you ship.

## Tier 1 pattern miner — M3 → M5

- **M3 — API endpoints + pipeline integration.** `GET /api/suggestions` (list pending), `POST /api/suggestions/{id}/accept` (apply to `search_overrides`), `POST /api/suggestions/{id}/dismiss`. Update `_should_keep` in `run.py` to union YAML `reject_title_keywords` with `db.get_search_override(search_id)["reject_keywords"]`. ~3h.
- **M4 — React Suggestions panel.** New `SuggestionsPanel.jsx` in `webapp/web/src/components/`. Header badge with pending count. Accept/dismiss buttons per suggestion. Fetches `/api/suggestions?search_id=<active>`. ~2-3h. Consider react-specialist subagent.
- **M5 — Cron entry for daily mining.** Snippet for user to install:
  ```
  0 9 * * * /Users/joshua/dev/find_my_ride/cron_run.sh --mine-suggestions
  ```
  Plus a quick verification: the existing 20 pending seats suggestions (in DB from this session's test run) should show in the UI once M3+M4 land.

## eBay recall verification

- Listing `https://www.ebay.co.uk/itm/198081180106` was confirmed in eBay's API results but did not arrive in this session's scrapes (3 of 4 user-flagged listings did — `197470493499`, `306729853634`, `305595442708`). After the next successful seats cron tick, verify `198081180106` is in `listings` table. If still missing after 2-3 cron ticks, deeper investigation needed (pagination order? newly-listed timestamp?).

## Per-search reject_reasons dropdown

Low-pri (user explicitly flagged). Currently `/api/config/reasons` returns a global list (cars-oriented) from `config.yaml`'s top-level `review.reject_reasons`. The seats hunt needs its own ("wrong listing type", "not a seat", etc.) — the cars list "rolling shell" / "4 door" / "parts only" is nonsense for parts. Shape: per-search reasons under `searches.<slug>.reject_reasons`, API accepts `?search_id=` and returns the appropriate list, React `RejectModal` (wherever it lives) plumbs the active `search_id` to the fetch.

## DE/NL seats category targeting

`searches.rs2000_mexico_seats.site_overrides.ebay.category_ids` currently has only `EBAY_GB: "33701"`. DE and NL marketplaces are deliberately skipped on the seats search until those cat IDs are mapped. Look them up via the eBay Taxonomy API (`/commerce/taxonomy/v1/category_tree/{77|146}/get_category_suggestions?q=seats`) when those markets become interesting for parts.

## Miner: skip-site existing-overrides check

In `core/miner.py` `_mine_skip_sites`: doesn't currently skip suggesting a site that's already in `search_overrides.skipped_sites_json`. Same pattern as `_existing_rejects` for `_mine_reject_keywords`. ~5 lines. Do before M3 ships so accepted skip-site suggestions don't get re-suggested.

## Tier 2 (parked)

- Ranking model — once Tier 1 saturates (most obvious patterns caught), build a per-search logistic regression / GBT over listing features → P(reject). Sort UI by P(keep) DESC. Wait until you have 100+ rejects per search for useful signal.

## Tier 3 (parked)

- LLM-in-the-loop scoring — for each new listing, send title + description + recent reject reasons + accepts/rejects examples to Claude → `{verdict, confidence, reasoning}`. Cache by URL. Where `user_note` text becomes high-value training material.

---

*Memory at `/Users/joshua/.claude/projects/-Users-joshua-dev-find-my-ride/memory/` carries durable preferences and project facts. This file carries open work threads.*
