"""Find (and optionally delete) leftover thumbnail files in OneDrive.

Before the resolver was fixed to always request full resolution, some runs
scraped Google Photos album grids and uploaded the small grid thumbnails
instead of the originals — 78-108 KB against 1-6 MB for a real photo. Those
runs also created extra numbered slots in event subfolders that no longer
correspond to any photo in the album.

Detection is by file size, which separates the two cases by more than a
factor of ten. Two safety rules constrain what can be deleted:

  1. Only files whose OneDrive URL appears in the dedupe DB are eligible —
     the sync uploaded those. Anything a person added by hand is invisible
     to this script.
  2. A folder is only considered when it also holds at least one full-size
     photo, so a folder of legitimately small images is never emptied.

Dry run by default; deletion requires --delete.

    python cleanup_orphans.py              # report only
    python cleanup_orphans.py --delete     # actually remove
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from helpers.onedrive import OneDriveClient
from sync import load_config, _configure_logging

log = logging.getLogger("cleanup_orphans")

# A real photo from these albums runs 1-6 MB; scraped grid thumbnails were
# 78-108 KB. 400 KB sits in the wide gap between the two populations.
THUMBNAIL_MAX_BYTES = 400 * 1024
# A folder must contain a photo at least this large before anything in it is
# treated as a leftover — guards against wiping a folder of small images.
FULL_SIZE_MIN_BYTES = 700 * 1024


def _uploaded_filenames(db_path: str) -> set:
    """Every filename the sync has recorded uploading, from the dedupe DB."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT filename FROM uploads WHERE filename IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        log.error("Could not read dedupe DB at %s: %s", db_path, exc)
        return set()
    return {r["filename"] for r in rows if r["filename"]}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or remove leftover thumbnail uploads in OneDrive"
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete the files listed (default is report only)",
    )
    parser.add_argument(
        "--max-thumbnail-bytes",
        type=int,
        default=THUMBNAIL_MAX_BYTES,
        help=f"Treat files at or below this size as leftovers (default {THUMBNAIL_MAX_BYTES})",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    _configure_logging(cfg.log_level)

    drive = OneDriveClient(
        tenant_id=cfg.azure_tenant,
        client_id=cfg.azure_client_id,
        client_secret=cfg.azure_client_secret,
        user_id=cfg.onedrive_user_id,
        drive_id=cfg.onedrive_drive_id,
        base_folder=cfg.onedrive_base_folder,
    )

    known = _uploaded_filenames(cfg.db_path)
    log.info("Dedupe DB lists %d uploaded filename(s)", len(known))
    if not known:
        log.error("Dedupe DB is empty or unreadable — refusing to touch anything")
        return 1

    log.info("Listing %s ...", cfg.onedrive_base_folder)
    files = drive.iter_files(cfg.onedrive_base_folder)
    log.info("Found %d file(s) in OneDrive", len(files))

    by_folder: Dict[str, List[dict]] = {}
    for f in files:
        by_folder.setdefault(f["path"].rsplit("/", 1)[0], []).append(f)

    candidates: List[dict] = []
    for folder, items in sorted(by_folder.items()):
        has_full_size = any(i["size"] >= FULL_SIZE_MIN_BYTES for i in items)
        if not has_full_size:
            continue
        small = [
            i
            for i in items
            if i["size"] <= args.max_thumbnail_bytes and i["name"] in known
        ]
        if not small:
            continue
        log.info("")
        log.info("%s", folder)
        for i in sorted(items, key=lambda x: x["name"]):
            flag = "  <-- leftover" if i in small else ""
            log.info("    %-46s %8.1f KB%s", i["name"], i["size"] / 1024, flag)
        candidates.extend(small)

    log.info("")
    log.info("=" * 66)
    if not candidates:
        log.info("No leftover thumbnails found — nothing to clean up.")
        return 0

    total_kb = sum(c["size"] for c in candidates) / 1024
    log.info("%d leftover file(s), %.1f KB total", len(candidates), total_kb)

    if not args.delete:
        log.info("")
        log.info("Dry run — nothing deleted. Re-run with --delete to remove these.")
        return 0

    deleted = 0
    for c in candidates:
        try:
            drive.delete_item(c["id"])
            log.info("Deleted %s", c["path"])
            deleted += 1
        except Exception as exc:
            log.error("Failed to delete %s: %s", c["path"], exc)

    log.info("")
    log.info("Deleted %d of %d file(s). They are in the OneDrive recycle bin.",
             deleted, len(candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
