import { useEffect, useState } from "react";
import { fetchSearches } from "../api.js";

/**
 * Fetches /api/searches on mount and renders a <select> that calls
 * onSearchChange(id, label) when the user picks a different search.
 */
export function SearchSwitcher({ searchId, onSearchChange }) {
  const [searches, setSearches] = useState([]);

  useEffect(() => {
    fetchSearches()
      .then(setSearches)
      .catch(() => setSearches([]));
  }, []);

  if (searches.length === 0) return null;

  return (
    <select
      value={searchId}
      onChange={(e) => {
        const id = Number(e.target.value);
        const found = searches.find((s) => s.id === id);
        onSearchChange(id, found?.label ?? "");
      }}
      className="px-2 py-1 border border-slate-300 rounded text-sm font-medium text-slate-700 bg-white"
      aria-label="Switch saved search"
    >
      {searches.map((s) => (
        <option key={s.id} value={s.id}>
          {s.label}
        </option>
      ))}
    </select>
  );
}
