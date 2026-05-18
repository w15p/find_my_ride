import { useEffect, useMemo, useState } from "react";
import * as api from "./api.js";
import { ListingCard } from "./components/ListingCard.jsx";
import { FilterBar } from "./components/FilterBar.jsx";
import { SearchSwitcher } from "./components/SearchSwitcher.jsx";
import { WatchedUrlsPanel } from "./components/WatchedUrlsPanel.jsx";

const DEFAULT_FILTERS = {
  status: "active",
  rejected: 0,
  canonical: 1,
  site: "",
  steering: "",
  q: "",
  min_usd: "",
  max_usd: "",
  year_min: "",
  year_max: "",
  sort: "scraped_at_desc",
};

/** Read ?search_id=N from the URL, returning N as a number or null. */
function searchIdFromUrl() {
  const raw = new URLSearchParams(window.location.search).get("search_id");
  const parsed = raw !== null ? Number(raw) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

/** Push a new URL with ?search_id=N without triggering a full navigation. */
function pushSearchId(id) {
  const params = new URLSearchParams(window.location.search);
  params.set("search_id", id);
  window.history.pushState({}, "", `${window.location.pathname}?${params.toString()}`);
}

export default function App() {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [items, setItems] = useState([]);
  const [count, setCount] = useState(0);
  const [reasons, setReasons] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // searchId defaults to URL param if present, otherwise 1.
  const [searchId, setSearchId] = useState(() => searchIdFromUrl() ?? 1);
  // searchLabel is populated when the SearchSwitcher reports back.
  const [searchLabel, setSearchLabel] = useState("");

  async function reload() {
    setLoading(true);
    setError(null);
    try {
      const [list, st] = await Promise.all([
        api.fetchListings(filters, searchId),
        api.fetchStats(),
      ]);
      setItems(list.items);
      setCount(list.count);
      setStats(st);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    api.fetchReasons().then(setReasons).catch(() => setReasons([]));
  }, []);

  // Seed the display title from the searches list using the initial searchId.
  useEffect(() => {
    api.fetchSearches().then((list) => {
      const found = list.find((s) => s.id === searchId);
      if (found) setSearchLabel(found.label);
    }).catch(() => {});
    // Intentionally only runs once on mount — the dropdown keeps label in sync
    // from that point on via onSearchChange.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-fetch whenever filters or searchId change.
  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters), searchId]);

  function onSearchChange(id, label) {
    setSearchId(id);
    setSearchLabel(label);
    pushSearchId(id);
    // Reset filters (especially site, which may not exist in the new search)
    // but keep them if the user hasn't touched them — brief says don't
    // redesign the filtering layer, so just leave filters as-is.
  }

  function onReject(url, reason, note) {
    api.reject(url, reason, note).then(reload).catch((e) => setError(e.message));
  }
  function onUnreject(url) {
    api.unreject(url).then(reload).catch((e) => setError(e.message));
  }
  function onNoteSave(url, note) {
    return api.setNote(url, note);
  }
  function onTogglePin(url, currentlyPinned) {
    const promise = currentlyPinned ? api.unpin(url) : api.pin(url);
    promise.then(reload).catch((e) => setError(e.message));
  }
  function onOverride(url, fields) {
    return api.setOverride(url, fields).then(reload);
  }

  const sites = useMemo(() => Object.keys(stats?.by_site || {}).sort(), [stats]);

  // Page title shown in the <header>. Once the SearchSwitcher loads its list
  // it calls onSearchChange which sets searchLabel; before that we show a
  // generic fallback so the header isn't blank on first paint.
  const displayTitle = searchLabel || "Find My Ride";

  return (
    <div className="min-h-screen">
      <header className="bg-white border-b border-slate-200 px-6 py-3 sticky top-0 z-10 shadow-sm">
        <div className="flex items-center justify-between gap-6">
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-bold text-red-700">{displayTitle}</h1>
            <SearchSwitcher searchId={searchId} onSearchChange={onSearchChange} />
          </div>
          <div className="text-sm text-slate-600 flex gap-3">
            {stats && (
              <>
                <span>{stats.active} active</span>
                <span>·</span>
                <span>{stats.sold} sold</span>
                <span>·</span>
                <span>{stats.rejected} rejected</span>
                <span>·</span>
                <span className="font-medium">{count} shown</span>
              </>
            )}
          </div>
        </div>
        <FilterBar
          filters={filters}
          setFilters={setFilters}
          sites={sites}
          onReset={() => setFilters(DEFAULT_FILTERS)}
        />
      </header>

      <WatchedUrlsPanel searchId={searchId} />

      {error && (
        <div className="bg-red-100 border border-red-400 text-red-800 px-4 py-2 m-4 rounded">
          {error}
        </div>
      )}

      <main className="max-w-7xl mx-auto px-4 py-6">
        {loading && items.length === 0 && (
          <div className="text-center text-slate-500 py-12">Loading…</div>
        )}
        {!loading && items.length === 0 && (
          <div className="text-center text-slate-500 py-12">
            <p className="text-lg font-medium mb-2">No listings yet.</p>
            <p className="text-sm">
              {searchLabel
                ? `The "${searchLabel}" search hasn't been scraped yet — run`
                : "Run"}
              {" "}
              <code className="bg-slate-100 px-1 rounded">python run.py</code>
              {" "}to populate.
            </p>
          </div>
        )}
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {items.map((l) => (
            <ListingCard
              key={l.url}
              listing={l}
              reasons={reasons}
              onReject={onReject}
              onUnreject={onUnreject}
              onNoteSave={onNoteSave}
              onTogglePin={onTogglePin}
              onOverride={onOverride}
            />
          ))}
        </div>
      </main>
    </div>
  );
}
