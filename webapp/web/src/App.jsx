import { useEffect, useMemo, useState } from "react";
import * as api from "./api.js";
import { ListingCard } from "./components/ListingCard.jsx";
import { FilterBar } from "./components/FilterBar.jsx";

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

export default function App() {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [items, setItems] = useState([]);
  const [count, setCount] = useState(0);
  const [reasons, setReasons] = useState([]);
  const [stats, setStats] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  async function reload() {
    setLoading(true);
    setError(null);
    try {
      const [list, st] = await Promise.all([
        api.fetchListings(filters),
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

  useEffect(() => {
    reload();
  }, [JSON.stringify(filters)]);

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

  return (
    <div className="min-h-screen">
      <header className="bg-white border-b border-slate-200 px-6 py-3 sticky top-0 z-10 shadow-sm">
        <div className="flex items-center justify-between gap-6">
          <h1 className="text-xl font-bold text-red-700">
            Escort Mk1 Review
          </h1>
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
            No listings match these filters.
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
