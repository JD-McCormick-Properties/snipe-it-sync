"""Image normalization helpers.

Currently:
  * Convert HEIC/HEIF to JPG so OneDrive previews work everywhere.
  * Compute a stable hash of image bytes for dedupe.
  * Build a deterministic upload filename per asset/index.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime, timezone
from typing import Tuple

from PIL import Image

log = logging.getLogger(__name__)

# pillow-heif registers a HEIF opener with PIL when imported.
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
except Exception as exc:  # pragma: no cover - environment-dependent
    log.warning("pillow-heif not available; HEIC files will be skipped: %s", exc)
    _HEIF_AVAILABLE = False


SAFE_TAG_RE = re.compile(r"[^A-Za-z0-9._-]+")
SAFE_NAME_RE = re.compile(r"[^\w\s._-]")

# Snipe-IT returns dates in several formats depending on version and locale.
_DT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%B %d, %Y %I:%M %p",
    "%B %d, %Y %I:%M%p",
]


def safe_asset_tag(asset_tag: str) -> str:
    """Sanitize an asset tag for use in folder/file names."""
    cleaned = SAFE_TAG_RE.sub("_", asset_tag.strip())
    return cleaned or "untagged"


def safe_name(name: str) -> str:
    """Sanitize a human-readable string for use in folder/file names.

    Keeps spaces, letters, numbers, hyphens, underscores, and periods.
    """
    cleaned = SAFE_NAME_RE.sub("", name.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_dt(dt_str: str) -> datetime:
    """Parse a Snipe-IT date string, falling back to now (UTC) on failure."""
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(dt_str.strip(), fmt)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def build_filename(model_name: str, uploader: str, dt_str: str, extension: str) -> str:
    """Return a filename like 'Silverado 1500 - John Smith - 2026-05-15 14-30-22.jpg'.

    Uploader is omitted when empty.
    """
    ext = extension.lower().lstrip(".") or "jpg"
    dt = parse_dt(dt_str) if dt_str else datetime.now(timezone.utc)
    dt_label = dt.strftime("%Y-%m-%d %H-%M-%S")

    parts = [safe_name(model_name) or "Photo"]
    if uploader:
        parts.append(safe_name(uploader))
    parts.append(dt_label)

    return f"{' - '.join(parts)}.{ext}"


def hash_bytes(content: bytes) -> str:
    """SHA-256 hex digest of image bytes."""
    return hashlib.sha256(content).hexdigest()


def normalize_image(
    content: bytes, mime_type: str, extension: str
) -> Tuple[bytes, str, str]:
    """Convert HEIC -> JPG. Pass other formats through untouched.

    Returns (content, mime_type, extension).
    """
    is_heic = (
        mime_type.lower() in {"image/heic", "image/heif"}
        or extension.lower() in {"heic", "heif"}
    )
    if not is_heic:
        return content, mime_type, extension

    if not _HEIF_AVAILABLE:
        # Pass through; uploader can decide whether to keep .heic
        log.warning(
            "HEIC content received but pillow-heif unavailable; uploading as-is"
        )
        return content, mime_type, extension

    try:
        img = Image.open(io.BytesIO(content))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=92, optimize=True)
        log.info("Converted HEIC -> JPG (%d bytes -> %d bytes)", len(content), out.tell())
        return out.getvalue(), "image/jpeg", "jpg"
    except Exception as exc:
        log.warning("HEIC conversion failed, uploading original bytes: %s", exc)
        return content, mime_type, extension
