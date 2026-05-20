import { useEffect, useState } from "react";
import { fetchWatched, addWatched, removeWatched } from "../api.js";

/** Compact section: paste-add input + list of watched URLs for the active
 * search. Used to bypass site-search recall failures (e.g. FB suppressing
 * a listing for cross-region location mismatch) by direct-fetching the URL
 * each cron tick. */
export function WatchedUrlsPanel({ searchId }) {
  const [items, setItems] = useState([]);
  const [collapsed, setCollapsed] = useState(true);
  const [input, setInput] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function reload() {
    try {
      const list = await fetchWatched(searchId);
      setItems(list);
      // Auto-expand the panel if the user has none yet — hints at the
      // feature's existence on first visit without nagging once they're
      // using it.
      if (list.length === 0) setCollapsed(false);
    } catch (e) {
      setError(e.message);
    }
  }

  useEffect(() => {
    if (searchId != null) reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchId]);

  async function onAdd(e) {
    e.preventDefault();
    const url = input.trim();
    if (!url) return;
    setBusy(true);
    setError(null);
    try {
      await addWatched(searchId, url);
      setInput("");
      await reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function onRemove(id) {
    setError(null);
    try {
      await removeWatched(id);
      await reload();
    } catch (e) {
      setError(e.message);
    }
  }

  const count = items.length;

  return (
    <section className="bg-amber-50 border-b border-amber-200 px-6 py-2">
      <div className="flex items-center justify-between">
        <button
          type="button"
          className="text-sm font-medium text-amber-900 hover:text-amber-700 flex items-center gap-2"
          onClick={() => setCollapsed((v) => !v)}
          aria-expanded={!collapsed}
        >
          <span>{collapsed ? "▸" : "▾"}</span>
          <span>Watched URLs ({count})</span>
          {count > 0 && (
            <span className="text-xs text-amber-700 font-normal">
              · pulled in directly each cron tick
            </span>
          )}
        </button>
      </div>

      {!collapsed && (
        <div className="mt-2 space-y-2">
          <form onSubmit={onAdd} className="flex gap-2">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="https://www.facebook.com/marketplace/item/…"
              className="flex-1 px-2 py-1 text-sm border border-amber-300 rounded bg-white"
              disabled={busy}
            />
            <button
              type="submit"
              disabled={busy || !input.trim()}
              className="px-3 py-1 text-sm font-medium bg-amber-700 text-white rounded hover:bg-amber-800 disabled:opacity-50"
            >
              {busy ? "Adding…" : "Add"}
            </button>
          </form>

          {error && (
            <div className="text-xs text-red-700 bg-red-50 border border-red-200 px-2 py-1 rounded">
              {error}
            </div>
          )}

          {count === 0 ? (
            <p className="text-xs text-amber-800">
              Paste a listing URL here when a known-good listing isn't surfacing in
              the regular feed — the scraper will fetch it directly each run.
              Useful for FB listings that get search-suppressed (cross-region
              mismatch, low engagement, etc.).
            </p>
          ) : (
            <ul className="space-y-1">
              {items.map((w) => (
                <WatchedRow key={w.id} item={w} onRemove={onRemove} />
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function WatchedRow({ item, onRemove }) {
  const statusBadge = statusInfo(item.last_status);
  return (
    <li className="flex items-center gap-3 text-sm bg-white border border-amber-200 rounded px-2 py-1">
      <span
        className={`text-xs px-1.5 py-0.5 rounded font-medium ${statusBadge.cls}`}
        title={statusBadge.title}
      >
        {statusBadge.label}
      </span>
      <a
        href={item.url}
        target="_blank"
        rel="noreferrer"
        className="text-blue-700 hover:underline truncate flex-1"
        title={item.url}
      >
        {item.listing_title || item.url}
      </a>
      {item.listing_price && (
        <span className="text-xs text-slate-600">{item.listing_price}</span>
      )}
      <button
        type="button"
        onClick={() => onRemove(item.id)}
        className="text-xs text-slate-500 hover:text-red-700"
        title="Stop watching"
      >
        ✕
      </button>
    </li>
  );
}

function statusInfo(status) {
  switch (status) {
    case "ok":
      return { label: "ok", cls: "bg-green-100 text-green-800", title: "Last fetch succeeded" };
    case "fetch_failed":
      return { label: "fail", cls: "bg-red-100 text-red-800", title: "Last fetch failed — listing may be removed" };
    case "session_invalid":
      return { label: "no auth", cls: "bg-orange-100 text-orange-800", title: "FB session logged out — run `python run.py --fb-login` to re-auth" };
    case "unsupported_site":
      return { label: "n/a", cls: "bg-slate-100 text-slate-700", title: "Site not yet supported for direct fetch" };
    default:
      return { label: "new", cls: "bg-amber-100 text-amber-800", title: "Not yet fetched — runs on next cron tick" };
  }
}
