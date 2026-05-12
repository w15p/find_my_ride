export function FilterBar({ filters, setFilters, sites, onReset }) {
  function patch(k, v) {
    setFilters((f) => ({ ...f, [k]: v }));
  }

  return (
    <div className="flex flex-wrap gap-2 items-center mt-3 text-sm">
      <input
        type="search"
        placeholder="Search title/description…"
        value={filters.q}
        onChange={(e) => patch("q", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded w-64"
      />
      <select
        value={filters.site}
        onChange={(e) => patch("site", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded"
      >
        <option value="">All sites</option>
        {sites.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>
      <select
        value={filters.status}
        onChange={(e) => patch("status", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded"
      >
        <option value="active">Active</option>
        <option value="sold">Sold</option>
        <option value="">All status</option>
      </select>
      <select
        value={String(filters.rejected)}
        onChange={(e) => patch("rejected", Number(e.target.value))}
        className="px-2 py-1 border border-slate-300 rounded"
      >
        <option value="0">Hide rejected</option>
        <option value="1">Only rejected</option>
        <option value="-1">All</option>
      </select>
      <select
        value={String(filters.canonical)}
        onChange={(e) => patch("canonical", Number(e.target.value))}
        className="px-2 py-1 border border-slate-300 rounded"
      >
        <option value="1">Canonical only</option>
        <option value="-1">Include dups</option>
        <option value="0">Only dups</option>
      </select>
      <select
        value={filters.steering}
        onChange={(e) => patch("steering", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded"
      >
        <option value="">Any drive</option>
        <option value="lhd">LHD</option>
        <option value="rhd">RHD</option>
        <option value="unknown">Unknown</option>
      </select>
      <input
        type="number"
        placeholder="Min $"
        value={filters.min_usd}
        onChange={(e) => patch("min_usd", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded w-20"
      />
      <input
        type="number"
        placeholder="Max $"
        value={filters.max_usd}
        onChange={(e) => patch("max_usd", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded w-20"
      />
      <select
        value={filters.sort}
        onChange={(e) => patch("sort", e.target.value)}
        className="px-2 py-1 border border-slate-300 rounded"
      >
        <option value="scraped_at_desc">Newest first</option>
        <option value="scraped_at_asc">Oldest first</option>
        <option value="price_asc">Price ↑</option>
        <option value="price_desc">Price ↓</option>
        <option value="year_desc">Year ↓</option>
      </select>
      <button
        onClick={onReset}
        className="px-2 py-1 border border-slate-300 rounded hover:bg-slate-100"
      >
        Reset
      </button>
    </div>
  );
}
