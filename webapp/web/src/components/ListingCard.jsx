import { useEffect, useRef, useState } from "react";

export function ListingCard({ listing, reasons, onReject, onUnreject, onNoteSave, onTogglePin, onOverride }) {
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
  const steeringOverridden = !!l.user_steering;
  const effectiveLocation = l.display_location || "";
  const locationOverridden = !!l.user_location;
  const [locationDraft, setLocationDraft] = useState(effectiveLocation);
  const [locationSaved, setLocationSaved] = useState(false);
  useEffect(() => { setLocationDraft(effectiveLocation); }, [effectiveLocation]);

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

        <div className="text-emerald-700 font-bold text-base">
          {l.price || "POA"}
          {usd && <span className="text-slate-500 font-normal text-xs ml-1">({usd})</span>}
        </div>

        <div className="text-xs text-slate-600 flex flex-wrap gap-x-3 gap-y-1 items-center">
          <span>Year: {l.year || "?"}</span>
          <label className="flex items-center gap-1">
            <span>Drive:</span>
            <select
              value={effectiveSteering.toLowerCase()}
              onChange={(e) => onOverride(l.url, { steering: e.target.value })}
              title={steeringOverridden ? "User-overridden" : "Auto-detected"}
              className={`text-xs border rounded px-1 py-0.5 ${
                steeringOverridden ? "border-blue-400 bg-blue-50" : "border-slate-200"
              } ${drive === "RHD" ? "text-amber-700" : ""}`}
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
          <p className="text-sm text-slate-700 line-clamp-3">{l.description}</p>
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
        </div>
      </div>
    </article>
  );
}
