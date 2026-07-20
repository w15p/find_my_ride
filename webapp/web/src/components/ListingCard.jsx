import { useEffect, useRef, useState } from "react";

// Currency options for the per-listing override dropdown. Keep in sync with
// core/currency.py SUPPORTED_CURRENCIES (and its symbol map). These cover the
// country allowlist: eurozone + GB + the non-euro EU/EEA/CH markets. usd_value
// on the backend converts all of them.
const CURRENCY_OPTIONS = [
  ["EUR", "EUR €"], ["GBP", "GBP £"], ["USD", "USD $"],
  ["DKK", "DKK kr"], ["SEK", "SEK kr"], ["NOK", "NOK kr"],
  ["PLN", "PLN zł"], ["CHF", "CHF"], ["CZK", "CZK Kč"],
  ["HUF", "HUF Ft"], ["RON", "RON lei"], ["BGN", "BGN лв"], ["ISK", "ISK kr"],
];

export function ListingCard({ listing, reasons, onReject, onUnreject, onNoteSave, onTogglePin, onOverride, onMarkActive }) {
  const l = listing;
  const [note, setNote] = useState(l.user_note || "");
  const [noteSaving, setNoteSaving] = useState(false);
  const [noteSaved, setNoteSaved] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);
  const [pickedReason, setPickedReason] = useState(reasons[0] || "");
  const [rejectComment, setRejectComment] = useState("");

  // Keep the picked-reason in sync when reasons load after first paint
  useEffect(() => {
    if (!pickedReason && reasons.length) setPickedReason(reasons[0]);
  }, [reasons, pickedReason]);

  // Autosave note on blur (only if it changed and isn't being edited)
  const noteBlurTimer = useRef(null);
  function onNoteBlur() {
    if ((note || "") === (l.user_note || "")) return;
    setNoteSaving(true);
    onNoteSave(l.url, note)
      .then(() => {
        setNoteSaved(true);
        clearTimeout(noteBlurTimer.current);
        noteBlurTimer.current = setTimeout(() => setNoteSaved(false), 1500);
      })
      .finally(() => setNoteSaving(false));
  }

  const rejected = !!l.user_rejected;
  const pinned = !!l.user_pinned;
  const usd = l.price_usd != null ? `$${l.price_usd.toLocaleString()}` : null;
  const effectiveSteering = (l.display_steering || "unknown");
  const drive = effectiveSteering.toUpperCase();
  // "Overridden" only fires when the user's value DIFFERS from the scraped
  // one. If a scraper backfill later produces the same value the user had
  // already entered (eBay's Drive Side aspect, etc.), the row stops showing
  // the blue tint and ↶ — the override is preserved in the DB but the visual
  // disagreement marker is silent.
  const steeringOverridden = !!l.user_steering &&
    (l.user_steering || "").toLowerCase() !== (l.steering || "").toLowerCase();
  const effectiveLocation = l.display_location || "";
  const locationOverridden = !!l.user_location && l.user_location !== l.location;
  const [locationDraft, setLocationDraft] = useState(effectiveLocation);
  const [locationSaved, setLocationSaved] = useState(false);
  useEffect(() => { setLocationDraft(effectiveLocation); }, [effectiveLocation]);

  const effectiveYear = l.display_year ?? "";
  const yearOverridden = l.user_year != null && l.user_year !== l.year;
  const [yearDraft, setYearDraft] = useState(String(effectiveYear));
  useEffect(() => { setYearDraft(String(effectiveYear)); }, [effectiveYear]);

  const effectiveCurrency = (l.display_currency || "").toUpperCase();
  const currencyOverridden = !!l.user_price_currency &&
    (l.user_price_currency || "").toUpperCase() !== (l.price_currency || "").toUpperCase();

  // Price-value override: input shows whole units ("17900"), API stores
  // cents internally (so 17900 → 1790000). Matches the year-input pattern
  // (always-input, blue border when overridden, ↶ to clear).
  const effectivePriceWhole = l.display_price_value != null
    ? String(Math.floor(l.display_price_value / 100))
    : "";
  const priceOverridden = l.user_price_value != null &&
    l.user_price_value !== l.price_value;
  const [priceDraft, setPriceDraft] = useState(effectivePriceWhole);
  useEffect(() => { setPriceDraft(effectivePriceWhole); }, [effectivePriceWhole]);
  const currencySymbol = ({
    EUR: "€", GBP: "£", USD: "$", DKK: "kr", SEK: "kr", NOK: "kr", ISK: "kr",
    PLN: "zł", CZK: "Kč", HUF: "Ft", RON: "lei", BGN: "лв",
  })[effectiveCurrency] || effectiveCurrency || "";

  // Amber-for-RHD is a "needs your attention" cue. Once the user manually
  // acknowledges the steering side, treat it as reviewed — no amber, even
  // if the value is still RHD.
  const userAcknowledgedSteering = !!l.user_steering;
  const showAmberRhd = drive === "RHD" && !userAcknowledgedSteering;

  const [descOpen, setDescOpen] = useState(false);
  const [showOriginal, setShowOriginal] = useState(false);
  useEffect(() => {
    if (!descOpen) return;
    const onKey = (e) => { if (e.key === "Escape") setDescOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [descOpen]);
  useEffect(() => { setShowOriginal(false); }, [descOpen]);

  // Card body and modal default to the English translation when present;
  // the original is one click away. `_LANG_NAMES` is intentionally English-
  // only — the UI is single-language for now.
  const _LANG_NAMES = { pt: "Portuguese", es: "Spanish", fr: "French",
                        de: "German", it: "Italian", nl: "Dutch",
                        ca: "Catalan", id: "Indonesian", en: "English",
                        gl: "Galician", ro: "Romanian" };
  const langName = _LANG_NAMES[l.description_language] || l.description_language;
  const hasTranslation = !!l.description_translated &&
                         l.description_translated !== l.description;
  const cardDescription = hasTranslation ? l.description_translated : l.description;
  const modalPrimary = showOriginal || !hasTranslation ? l.description : l.description_translated;

  return (
    <article
      className={`bg-white rounded-lg border ${
        rejected
          ? "border-red-300 opacity-70"
          : pinned
          ? "border-amber-400 ring-2 ring-amber-200"
          : "border-slate-200"
      } shadow-sm overflow-hidden flex flex-col`}
    >
      <div className="relative bg-slate-100 aspect-[16/10]">
        {l.image_url ? (
          <a href={l.image_url} target="_blank" rel="noopener noreferrer">
            <img
              src={`/api/image?url=${encodeURIComponent(l.image_url)}`}
              alt={l.title}
              loading="lazy"
              className="w-full h-full object-cover"
              onError={(e) => (e.currentTarget.style.display = "none")}
            />
          </a>
        ) : (
          <div className="w-full h-full flex items-center justify-center text-slate-400 text-sm">
            No image
          </div>
        )}
        <span className="absolute top-2 left-2 bg-slate-800/80 text-white text-xs px-2 py-0.5 rounded">
          {l.site_name}
        </span>
        <button
          onClick={() => onTogglePin(l.url, pinned)}
          aria-label={pinned ? "Unpin" : "Pin to top"}
          title={pinned ? "Pinned — click to unpin" : "Pin to top"}
          className={`absolute top-1.5 ${rejected ? "right-1.5" : "right-1.5"} w-8 h-8 flex items-center justify-center rounded-full transition ${
            pinned
              ? "bg-amber-400 text-white hover:bg-amber-500"
              : "bg-white/85 text-slate-500 hover:bg-white hover:text-amber-500"
          }`}
        >
          {pinned ? "★" : "☆"}
        </button>
        {rejected && (
          <span className="absolute bottom-2 right-2 bg-red-600 text-white text-xs px-2 py-0.5 rounded">
            ✗ {l.user_reject_reason || "rejected"}
          </span>
        )}
        {l.status === "sold" && !rejected && (
          <span className="absolute bottom-2 right-2 bg-slate-700 text-white text-xs px-2 py-0.5 rounded">
            SOLD
          </span>
        )}
        {l.status === "expired" && !rejected && (
          <span className="absolute bottom-2 right-2 bg-slate-500 text-white text-xs px-2 py-0.5 rounded">
            EXPIRED
          </span>
        )}
      </div>

      <div className="p-3 flex-1 flex flex-col gap-2">
        <div className="flex items-start justify-between gap-2">
          <a
            href={l.url}
            target="_blank"
            rel="noopener noreferrer"
            className="font-semibold text-slate-800 hover:underline leading-tight"
          >
            {l.title}
          </a>
        </div>

        <div className="text-emerald-700 font-bold text-base flex items-center gap-1 flex-wrap">
          {currencySymbol && <span>{currencySymbol}</span>}
          <input
            type="text"
            inputMode="numeric"
            value={priceDraft}
            placeholder={l.price || "POA"}
            onChange={(e) => setPriceDraft(e.target.value)}
            onBlur={() => {
              if (priceDraft === effectivePriceWhole) return;
              onOverride(l.url, { price_value: priceDraft }).catch(() => {
                setPriceDraft(effectivePriceWhole);
              });
            }}
            onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); }}
            title={priceOverridden ? "Price overridden — click ↶ to restore scraped value" : "Click to edit"}
            className={`text-base font-bold text-emerald-700 border rounded px-1 py-0 w-24 ${
              priceOverridden
                ? "border-blue-400 bg-blue-50"
                : "border-transparent hover:border-slate-200 focus:border-slate-300 bg-transparent"
            }`}
          />
          {priceOverridden && (
            <button
              onClick={() => onOverride(l.url, { price_value: "" })}
              title="Clear override (restore scraped price)"
              className="text-[10px] text-slate-400 hover:text-slate-700"
            >
              ↶
            </button>
          )}
          {usd && <span className="text-slate-500 font-normal text-xs">({usd})</span>}
          {l.price_direction && (
            <span
              className={`font-bold text-sm ${
                l.price_direction === "down" ? "text-green-600" : "text-red-600"
              }`}
              title={
                l.prev_display_price
                  ? `Price ${l.price_direction === "down" ? "dropped" : "rose"} from ${l.prev_display_price}` +
                    (l.price_changed_at ? ` (${l.price_changed_at.slice(0, 10)})` : "")
                  : "Price changed since last check"
              }
            >
              {l.price_direction === "down" ? "↓" : "↑"}
            </span>
          )}
          {/* Always render so a listing with a missing or unsupported
              currency (e.g. FB reporting DKK on a EUR-priced Danish car,
              or no currency at all) can still be corrected. */}
          <>
            <select
              value={effectiveCurrency || ""}
              onChange={(e) => onOverride(l.url, { price_currency: e.target.value })}
              title={currencyOverridden ? "Currency overridden — click ↶ to restore" : "Wrong or missing currency? Pick the correct one"}
              className={`text-[11px] border rounded px-1 font-normal cursor-pointer ${
                currencyOverridden ? "border-blue-400 bg-blue-50 text-slate-700"
                  : !effectiveCurrency ? "border-amber-400 bg-amber-50 text-slate-700"
                  : "border-transparent text-slate-400 hover:border-slate-300 hover:text-slate-600"
              }`}
            >
              <option value="">— set currency —</option>
              {CURRENCY_OPTIONS.map(([code, label]) => (
                <option key={code} value={code}>{label}</option>
              ))}
              {/* Preserve an unexpected current value so the select shows it
                  correctly and picking a standard code registers as a change. */}
              {effectiveCurrency && !CURRENCY_OPTIONS.some(([c]) => c === effectiveCurrency) && (
                <option value={effectiveCurrency}>{effectiveCurrency}</option>
              )}
            </select>
            {currencyOverridden && (
              <button
                onClick={() => onOverride(l.url, { price_currency: "" })}
                title="Clear override (restore scraped currency)"
                className="text-[10px] text-slate-400 hover:text-slate-700"
              >
                ↶
              </button>
            )}
          </>
        </div>

        <div className="text-xs text-slate-600 flex flex-wrap gap-x-3 gap-y-1 items-center">
          <label className="flex items-center gap-1">
            <span>Year:</span>
            <input
              type="text"
              inputMode="numeric"
              value={yearDraft}
              placeholder="?"
              onChange={(e) => setYearDraft(e.target.value)}
              onBlur={() => {
                if (yearDraft === String(effectiveYear || "")) return;
                onOverride(l.url, { year: yearDraft }).catch(() => {
                  setYearDraft(String(effectiveYear || ""));
                });
              }}
              title={yearOverridden ? "User-overridden" : "Auto-detected"}
              className={`w-14 text-xs border rounded px-1 py-0.5 ${
                yearOverridden ? "border-blue-400 bg-blue-50" : "border-slate-200"
              }`}
            />
            {yearOverridden && (
              <button
                onClick={() => onOverride(l.url, { year: "" })}
                title="Clear override (restore auto-detected value)"
                className="text-[10px] text-slate-400 hover:text-slate-700"
              >
                ↶
              </button>
            )}
          </label>
          <label className="flex items-center gap-1">
            <span>Drive:</span>
            <select
              value={effectiveSteering.toLowerCase()}
              onChange={(e) => onOverride(l.url, { steering: e.target.value })}
              title={steeringOverridden ? "User-overridden" : "Auto-detected"}
              className={`text-xs border rounded px-1 py-0.5 ${
                steeringOverridden ? "border-blue-400 bg-blue-50" : "border-slate-200"
              } ${showAmberRhd ? "text-amber-700" : ""}`}
            >
              <option value="lhd">LHD</option>
              <option value="rhd">RHD</option>
              <option value="unknown">?</option>
            </select>
            {steeringOverridden && (
              <button
                onClick={() => onOverride(l.url, { steering: "" })}
                title="Clear override (restore auto-detected value)"
                className="text-[10px] text-slate-400 hover:text-slate-700"
              >
                ↶
              </button>
            )}
          </label>
        </div>
        <label className="text-xs text-slate-600 flex items-center gap-1">
          <span>Location:</span>
          <input
            type="text"
            value={locationDraft}
            placeholder="(unknown)"
            onChange={(e) => { setLocationDraft(e.target.value); setLocationSaved(false); }}
            onBlur={() => {
              if ((locationDraft || "") === (effectiveLocation || "")) return;
              onOverride(l.url, { location: locationDraft }).then(() => {
                setLocationSaved(true);
                setTimeout(() => setLocationSaved(false), 1500);
              });
            }}
            title={locationOverridden ? "User-overridden" : "Auto-detected"}
            className={`flex-1 text-xs border rounded px-1 py-0.5 min-w-0 ${
              locationOverridden ? "border-blue-400 bg-blue-50" : "border-slate-200"
            }`}
          />
          {locationOverridden && (
            <button
              onClick={() => onOverride(l.url, { location: "" })}
              title="Clear override (restore auto-detected value)"
              className="text-[10px] text-slate-400 hover:text-slate-700"
            >
              ↶
            </button>
          )}
          {locationSaved && <span className="text-[10px] text-emerald-600">saved</span>}
        </label>

        {l.description && (
          <p
            className="text-sm text-slate-700 line-clamp-3 cursor-pointer hover:text-slate-900"
            onClick={() => setDescOpen(true)}
            title={hasTranslation
              ? `Click to read full description (translated from ${langName})`
              : "Click to read full description"}
          >
            {cardDescription}
            {hasTranslation && (
              <span className="ml-1 text-[10px] text-slate-400">[{langName} → EN]</span>
            )}
          </p>
        )}
        {descOpen && (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
            onClick={() => setDescOpen(false)}
          >
            <div
              className="bg-white rounded-lg max-w-2xl w-full max-h-[80vh] overflow-hidden flex flex-col shadow-xl"
              onClick={(e) => e.stopPropagation()}
            >
              <div className="px-4 py-3 border-b border-slate-200 flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <a
                    href={l.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="font-semibold text-slate-800 hover:underline block truncate"
                  >
                    {l.title}
                  </a>
                  <div className="text-xs text-slate-500 mt-0.5">
                    {l.site_name} · {l.price || "POA"} · {effectiveYear || "?"} · {effectiveLocation || "?"}
                  </div>
                </div>
                <button
                  onClick={() => setDescOpen(false)}
                  aria-label="Close"
                  className="text-slate-400 hover:text-slate-700 text-2xl leading-none px-1"
                >
                  ×
                </button>
              </div>
              {hasTranslation && (
                <div className="px-4 pt-2 text-xs text-slate-500 flex items-center gap-2">
                  <span>
                    {showOriginal ? `Original (${langName})` : `Translated from ${langName}`}
                  </span>
                  <button
                    onClick={() => setShowOriginal(!showOriginal)}
                    className="text-blue-600 hover:underline"
                  >
                    {showOriginal ? "Show English" : "Show original"}
                  </button>
                </div>
              )}
              <div className="px-4 py-3 overflow-y-auto whitespace-pre-wrap text-sm text-slate-800">
                {modalPrimary}
              </div>
            </div>
          </div>
        )}

        {l.also_on && l.also_on.length > 0 && (
          <div className="text-xs text-slate-500">
            Also on:{" "}
            {l.also_on.map((d, i) => (
              <span key={d.url}>
                <a href={d.url} target="_blank" rel="noopener noreferrer" className="hover:underline">
                  {d.site_name}
                </a>
                {i < l.also_on.length - 1 ? ", " : ""}
              </span>
            ))}
          </div>
        )}

        <textarea
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onBlur={onNoteBlur}
          placeholder="Notes (autosaves)"
          className="mt-1 w-full text-sm border border-slate-200 rounded px-2 py-1 min-h-[2.5rem] focus:outline-none focus:border-slate-400"
        />
        <div className="text-[10px] text-slate-400 h-3">
          {noteSaving ? "Saving…" : noteSaved ? "Saved" : " "}
        </div>

        <div className="mt-auto pt-2 border-t border-slate-100 flex items-center gap-2">
          {!rejected && !rejectOpen && (
            <button
              onClick={() => setRejectOpen(true)}
              className="text-sm px-3 py-1 bg-red-600 text-white rounded hover:bg-red-700"
            >
              Reject
            </button>
          )}
          {!rejected && rejectOpen && (
            <div className="flex items-center gap-1 flex-wrap">
              <select
                value={pickedReason}
                onChange={(e) => setPickedReason(e.target.value)}
                className="text-sm border border-slate-300 rounded px-2 py-1"
              >
                {reasons.map((r) => (
                  <option key={r} value={r}>{r}</option>
                ))}
              </select>
              <input
                type="text"
                placeholder="optional comment"
                value={rejectComment}
                onChange={(e) => setRejectComment(e.target.value)}
                className="text-sm border border-slate-300 rounded px-2 py-1 w-32"
              />
              <button
                onClick={() => {
                  onReject(l.url, pickedReason, rejectComment ? `${note ? note + "\n" : ""}${rejectComment}` : note || null);
                  setRejectOpen(false);
                  setRejectComment("");
                }}
                className="text-sm px-2 py-1 bg-red-600 text-white rounded hover:bg-red-700"
              >
                Confirm
              </button>
              <button
                onClick={() => { setRejectOpen(false); setRejectComment(""); }}
                className="text-sm px-2 py-1 border border-slate-300 rounded hover:bg-slate-100"
              >
                Cancel
              </button>
            </div>
          )}
          {rejected && (
            <button
              onClick={() => onUnreject(l.url)}
              className="text-sm px-3 py-1 border border-slate-300 rounded hover:bg-slate-100"
            >
              Un-reject
            </button>
          )}
          {(l.status === "sold" || l.status === "expired") && onMarkActive && (
            <button
              onClick={() => onMarkActive(l.url)}
              title="Validate flagged this as sold/expired. Click to put it back in the active feed."
              className="text-sm px-3 py-1 border border-slate-300 rounded hover:bg-slate-100"
            >
              Mark active
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
