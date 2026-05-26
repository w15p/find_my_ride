"""Disk cache for proxied listing images.

Used by:
- webapp/api.py's /api/image proxy — cache on the first successful proxy,
  serve from disk on subsequent requests (so images survive FB CDN URL
  expiry).
- run.py's cmd_refresh_fb_images — prefetch into the cache as fresh URLs
  are discovered, so the review UI never sees a first-request 403 even
  if it loads between refresh cycles.

Storage layout: cache/images/<2-char-shard>/<sha256-of-url>.<ext>
Atomic writes via tempfile + os.replace.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterable, Optional

# Resolve relative to the repo root regardless of CWD.
_REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = _REPO_ROOT / "cache" / "images"

# Content-Type → file extension mapping. Round-trips both ways so cache
# reads can recover the right media type from the on-disk extension.
EXT_BY_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg":  "jpg",
    "image/png":  "png",
    "image/webp": "webp",
    "image/gif":  "gif",
    "image/avif": "avif",
}
TYPE_BY_EXT = {v: k for k, v in EXT_BY_TYPE.items()}


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def path_for(url: str, ext: str) -> Path:
    """Return the canonical cache path for an image URL + extension."""
    key = _key(url)
    return CACHE_DIR / key[:2] / f"{key}.{ext}"


def find(url: str) -> Optional[tuple[Path, str]]:
    """Look up a previously-cached image. Returns (path, media_type) on hit,
    None on miss. Globs because we don't know the extension at lookup
    time without remembering the original Content-Type."""
    key = _key(url)
    shard = CACHE_DIR / key[:2]
    if not shard.exists():
        return None
    for match in shard.glob(f"{key}.*"):
        ext = match.suffix.lstrip(".").lower()
        media_type = TYPE_BY_EXT.get(ext, "application/octet-stream")
        return (match, media_type)
    return None


def write(url: str, content_type: str, byte_chunks: Iterable[bytes]) -> Path:
    """Atomically write `byte_chunks` to the cache for `url`. Returns the
    final cache path. Raises on IO failure (caller decides whether to
    fall back to streaming or just log + give up)."""
    ext = EXT_BY_TYPE.get(content_type, "bin")
    final_path = path_for(url, ext)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    # tempfile in the same dir → os.replace is atomic on the same filesystem.
    fd, tmp_name = tempfile.mkstemp(dir=final_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in byte_chunks:
                if chunk:
                    out.write(chunk)
        os.replace(tmp_name, final_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return final_path
