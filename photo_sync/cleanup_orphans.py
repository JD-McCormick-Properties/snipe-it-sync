"""Find (and optionally delete) redundant files the sync left in OneDrive.

Two independent problems, each with its own pass:

``thumbnails``
    Before the resolver was fixed to always request full resolution, some
    runs uploaded Google Photos grid thumbnails instead of the originals —
    78-108 KB against 1-6 MB for a real photo. Detected by size.

``duplicates``
    Earlier still, ``parse_dt`` fell back to ``datetime.now()`` when it
    couldn't read an activity date, so every run stamped the filename with
    the run time rather than the event time and wrote the same photo again
    under a new name. Detected by downloading candidates and comparing
    perceptual hashes.

Safety rules, both passes:

  * Only filenames matching the sync's own naming patterns are eligible, so
    anything a person added by hand is never a candidate.
  * Grouping never crosses folder boundaries — a photo that legitimately
    appears in both a flat model folder and an event subfolder keeps both.
  * A duplicate group always retains one file; the script cannot empty a
    group, and it keeps the earliest copy.
  * Dry run by default. Deleting requires --delete, and deleted files go to
    the OneDrive recycle bin rather than being purged.

    python cleanup_orphans.py                      # report both passes
    python cleanup_orphans.py --pass duplicates    # one pass only
    python cleanup_orphans.py --delete             # actually remove
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from helpers.image_utils import perceptual_hash
from helpers.onedrive import OneDriveClient
from sync import load_config, _configure_logging

log = logging.getLogger("cleanup_orphans")

# A real photo runs 1-6 MB; scraped grid thumbnails were 78-108 KB. 400 KB
# sits in the wide gap between the two populations.
THUMBNAIL_MAX_BYTES = 400 * 1024
# A folder needs a photo at least this large before anything in it is treated
# as a leftover thumbnail — guards against wiping a folder of small images.
FULL_SIZE_MIN_BYTES = 700 * 1024
# Same threshold the sync uses to call two images the same photo.
PERCEPTUAL_MAX_DISTANCE = 20

# Filenames the sync itself produces. Anything else is presumed human-created
# and is never a deletion candidate.
#   "Model - 2026-07-02 06-03-58.jpg"
#   "Model - Nick Brown - 2026-07-02 06-03-58.jpg"
#   "Model - 3.jpg"            (inside an event subfolder)
SYNC_NAME_RE = re.compile(
    r"^.+ - (?:\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}|\d+)\.[A-Za-z0-9]+$"
)
# Trailing "YYYY-MM-DD HH-MM-SS" used to pick which copy to keep.
STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})\.[A-Za-z0-9]+$")


def _sync_named(name: str) -> bool:
    return bool(SYNC_NAME_RE.match(name))


def _sort_key(item: dict) -> tuple:
    """Earliest first — by filename timestamp, falling back to mtime."""
    m = STAMP_RE.search(item["name"])
    return (m.group(1) if m else "", item.get("modified", ""), item["name"])


def _uploaded_filenames(db_path: str) -> set:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT filename FROM uploads WHERE filename IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error as exc:
        log.warning("Could not read dedupe DB at %s: %s", db_path, exc)
        return set()
    return {r["filename"] for r in rows if r["filename"]}


def find_thumbnails(
    by_folder: Dict[str, List[dict]], known: set, max_bytes: int
) -> List[dict]:
    """Leftover grid thumbnails, identified by size."""
    out: List[dict] = []
    for folder, items in sorted(by_folder.items()):
        if not any(i["size"] >= FULL_SIZE_MIN_BYTES for i in items):
            continue
        small = [
            i
            for i in items
            if i["size"] <= max_bytes and (i["name"] in known or _sync_named(i["name"]))
        ]
        if not small:
            continue
        log.info("")
        log.info("%s", folder)
        for i in sorted(items, key=lambda x: x["name"]):
            mark = "  <-- thumbnail" if i in small else ""
            log.info("    %-52s %8.1f KB%s", i["name"], i["size"] / 1024, mark)
        out.extend(small)
    return out


def find_duplicates(
    by_folder: Dict[str, List[dict]], drive: OneDriveClient
) -> List[dict]:
    """Same photo stored repeatedly in one folder under different names.

    Only files whose byte size matches another file in the same folder are
    downloaded — identical re-uploads are byte-identical or near enough, so
    this skips the bulk of the drive while still catching every real group.
    """
    out: List[dict] = []

    for folder, items in sorted(by_folder.items()):
        eligible = [i for i in items if _sync_named(i["name"])]
        if len(eligible) < 2:
            continue

        by_size: Dict[int, List[dict]] = defaultdict(list)
        for i in eligible:
            by_size[i["size"]].append(i)
        suspects = [i for group in by_size.values() if len(group) > 1 for i in group]
        if not suspects:
            continue

        log.info("")
        log.info("%s  (%d files, %d share a size)", folder, len(items), len(suspects))

        hashed: List[tuple] = []
        for i in suspects:
            content = drive.download_item(i["id"])
            if not content:
                continue
            ph = perceptual_hash(content)
            if ph:
                hashed.append((i, ph))

        groups: List[List[tuple]] = []
        for item, ph in hashed:
            target = int(ph, 16)
            for g in groups:
                if bin(target ^ int(g[0][1], 16)).count("1") <= PERCEPTUAL_MAX_DISTANCE:
                    g.append((item, ph))
                    break
            else:
                groups.append([(item, ph)])

        for g in groups:
            if len(g) < 2:
                continue
            ordered = sorted((i for i, _ in g), key=_sort_key)
            keep, drop = ordered[0], ordered[1:]
            log.info("    same photo x%d  (%.1f KB each)", len(g), keep["size"] / 1024)
            log.info("      keep   %s", keep["name"])
            for d in drop:
                log.info("      remove %s", d["name"])
            out.extend(drop)

    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Report or remove redundant sync uploads in OneDrive"
    )
    parser.add_argument("--delete", action="store_true",
                        help="Actually delete (default is report only)")
    parser.add_argument("--pass", dest="which", default="both",
                        choices=["both", "thumbnails", "duplicates"],
                        help="Which detection pass to run")
    parser.add_argument("--max-thumbnail-bytes", type=int, default=THUMBNAIL_MAX_BYTES)
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
    log.info("Listing %s ...", cfg.onedrive_base_folder)
    files = drive.iter_files(cfg.onedrive_base_folder)
    log.info("Found %d file(s) in OneDrive", len(files))
    if not files:
        log.error("Nothing listed — check ONEDRIVE_BASE_FOLDER")
        return 1

    by_folder: Dict[str, List[dict]] = defaultdict(list)
    for f in files:
        by_folder[f["path"].rsplit("/", 1)[0]].append(f)

    candidates: List[dict] = []
    if args.which in ("both", "thumbnails"):
        log.info("")
        log.info("--- leftover thumbnails " + "-" * 42)
        candidates += find_thumbnails(by_folder, known, args.max_thumbnail_bytes)
    if args.which in ("both", "duplicates"):
        log.info("")
        log.info("--- duplicate photos " + "-" * 45)
        candidates += find_duplicates(by_folder, drive)

    # A file could be flagged by both passes; delete it once.
    unique: Dict[str, dict] = {c["id"]: c for c in candidates}
    candidates = list(unique.values())

    log.info("")
    log.info("=" * 66)
    if not candidates:
        log.info("Nothing to clean up.")
        return 0

    log.info("%d file(s) proposed for removal, %.1f MB total",
             len(candidates), sum(c["size"] for c in candidates) / 1024 / 1024)
    log.info("%d file(s) in OneDrive would remain", len(files) - len(candidates))

    if not args.delete:
        log.info("")
        log.info("Dry run — nothing deleted. Re-run with --delete to remove these.")
        return 0

    # The credentials can reach the whole drive, and delete_item takes a bare
    # item id. Re-check every path against the configured base folder so a
    # candidate from outside it can never be deleted, whatever produced it.
    base = cfg.onedrive_base_folder.strip("/")
    outside = [c for c in candidates if not c["path"].startswith(f"{base}/")]
    if outside:
        log.error("Refusing to delete — %d candidate(s) outside %s:", len(outside), base)
        for c in outside[:10]:
            log.error("    %s", c["path"])
        return 1

    deleted = 0
    for c in candidates:
        try:
            drive.delete_item(c["id"])
            deleted += 1
        except Exception as exc:
            log.error("Failed to delete %s: %s", c["path"], exc)

    log.info("")
    log.info("Deleted %d of %d file(s) — recoverable from the OneDrive recycle bin.",
             deleted, len(candidates))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
