// Thin wrapper around fetch — single place to swap base URL or attach errors.

const BASE = "";  // proxied by Vite in dev; same-origin when served by FastAPI.

async function jsonOrThrow(resp) {
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${resp.status}: ${text}`);
  }
  return resp.json();
}

export async function fetchSearches() {
  return jsonOrThrow(await fetch(`${BASE}/api/searches`));
}

export async function fetchListings(filters, searchId) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== "" && v !== null && v !== undefined) qs.set(k, v);
  }
  if (searchId != null) qs.set("search_id", searchId);
  return jsonOrThrow(await fetch(`${BASE}/api/listings?${qs.toString()}`));
}

export async function fetchReasons() {
  return jsonOrThrow(await fetch(`${BASE}/api/config/reasons`));
}

export async function fetchStats() {
  return jsonOrThrow(await fetch(`${BASE}/api/stats`));
}

export async function reject(url, reason, note) {
  return jsonOrThrow(
    await fetch(`${BASE}/api/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, reason, note }),
    })
  );
}

export async function unreject(url) {
  return jsonOrThrow(
    await fetch(`${BASE}/api/unreject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    })
  );
}

export async function setNote(url, note) {
  return jsonOrThrow(
    await fetch(`${BASE}/api/note`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, note }),
    })
  );
}

export async function pin(url) {
  return jsonOrThrow(
    await fetch(`${BASE}/api/pin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    })
  );
}

export async function unpin(url) {
  return jsonOrThrow(
    await fetch(`${BASE}/api/unpin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    })
  );
}

export async function setOverride(url, fields) {
  // fields = { steering?: "lhd"|"rhd"|"unknown"|"", location?: string }
  return jsonOrThrow(
    await fetch(`${BASE}/api/override`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, ...fields }),
    })
  );
}
