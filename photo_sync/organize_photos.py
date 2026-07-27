"""Move loose photos in model folders into their event subfolders.

The sync only creates an event subfolder when a single check-in or check-out
produced more than one photo (``use_subfolder = batch_size > 1``). A checkout
with exactly one photo therefore lands flat in the model folder, with the
event recorded in the filename instead of the folder name. That is why a
vehicle folder holds a mix of loose files and "Check Out - ..." folders.

This script reclassifies the loose files:

  "Model - Nick Brown - 2026-07-17 09-01-45.jpg"
      carries an uploader, so it came from an activity entry. Matched back to
      that entry by name and timestamp to recover whether it was a check-in
      or a check-out, then moved into the matching event subfolder.

  "Model - 2026-06-01 18-43-50.jpg"
      no uploader, so it came from the asset image field or the top-level
      notes rather than any event. There is no event to file it under, so it
      is reported and left alone.

Dry run by default; --apply performs the moves. Moves are reparent
operations, so file contents and OneDrive version history are preserved.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from helpers.dedupe import PERCEPTUAL_MAX_DISTANCE
from helpers.image_utils import parse_dt, perceptual_hash, safe_name
from helpers.onedrive import OneDriveClient
from helpers.snipeit import SnipeITClient, summarize_asset
from sync import (
    _action_label,
    _configure_logging,
    _event_subfolder_name,
    _extract_entry_date,
    _extract_uploader_name,
    load_config,
)

log = logging.getLogger("organize_photos")

# "<model> - <uploader> - <YYYY-MM-DD HH-MM-SS>.<ext>"
WITH_UPLOADER_RE = re.compile(
    r"^(?P<model>.+?) - (?P<uploader>.+?) - "
    r"(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})\.(?P<ext>[A-Za-z0-9]+)$"
)
# "<model> - <YYYY-MM-DD HH-MM-SS>.<ext>"
NO_UPLOADER_RE = re.compile(
    r"^(?P<model>.+?) - (?P<stamp>\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2})\.(?P<ext>[A-Za-z0-9]+)$"
)

# The filename stamp is the event date, so the match should be near exact.
MATCH_WINDOW = timedelta(minutes=2)


def _activity_index(
    snipe: SnipeITClient, asset_ids: List[int]
) -> List[dict]:
    """Every check-in/check-out entry for the given assets."""
    entries: List[dict] = []
    for aid in asset_ids:
        try:
            for e in snipe.iter_asset_activity(aid):
                action = (e.get("action_type") or "").lower()
                if "checkin" in action or "checkout" in action:
                    entries.append(e)
        except Exception as exc:
            log.warning("  activity fetch failed for asset %s: %s", aid, exc)
    return entries


def _find_entry(entries: List[dict], uploader: str, stamp: str) -> Optional[dict]:
    """Match a filename's uploader and timestamp back to its activity entry."""
    # filenames carry HH-MM-SS; parse_dt expects HH:MM:SS
    day, clock = stamp.split(" ")
    want = parse_dt(f"{day} {clock.replace('-', ':')}")
    best, best_delta = None, None
    for e in entries:
        if safe_name(_extract_uploader_name(e)) != uploader:
            continue
        ed = _extract_entry_date(e)
        if not ed:
            continue
        delta = abs(parse_dt(ed) - want)
        if delta <= MATCH_WINDOW and (best_delta is None or delta < best_delta):
            best, best_delta = e, delta
    return best


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="File loose model-folder photos into their event subfolders"
    )
    ap.add_argument("--apply", action="store_true",
                    help="Actually move the files (default reports only)")
    args = ap.parse_args(argv)

    cfg = load_config()
    _configure_logging(cfg.log_level)

    drive = OneDriveClient(
        tenant_id=cfg.azure_tenant, client_id=cfg.azure_client_id,
        client_secret=cfg.azure_client_secret, user_id=cfg.onedrive_user_id,
        drive_id=cfg.onedrive_drive_id, base_folder=cfg.onedrive_base_folder,
    )
    snipe = SnipeITClient(cfg.snipe_url, cfg.snipe_token)

    log.info("Mapping Snipe-IT assets to folders ...")
    folder_assets: Dict[str, List[int]] = defaultdict(list)
    for asset in snipe.iter_hardware():
        info = summarize_asset(asset)
        folder_assets[drive.asset_folder(info["category_name"], info["model_name"])].append(info["id"])
    log.info("%d model folder(s) from Snipe-IT", len(folder_assets))

    base = cfg.onedrive_base_folder.strip("/")
    log.info("Listing %s ...", base)
    files = drive.iter_files(base)
    log.info("%d file(s) in OneDrive", len(files))

    # Loose = directly inside a model folder: <base>/<category>/<model>/<file>
    loose: Dict[str, List[dict]] = defaultdict(list)
    nested = 0
    for f in files:
        rel = f["path"][len(base):].strip("/")
        parts = rel.split("/")
        if len(parts) == 3:
            loose["/".join([base] + parts[:2])].append(f)
        else:
            nested += 1

    total_loose = sum(len(v) for v in loose.values())
    log.info("")
    log.info("%d file(s) already in event subfolders", nested)
    log.info("%d loose file(s) across %d model folder(s)", total_loose, len(loose))

    moves: List[tuple] = []
    no_event: List[dict] = []
    unmatched: List[dict] = []

    for folder, items in sorted(loose.items()):
        asset_ids = folder_assets.get(folder, [])
        log.info("")
        log.info("%s  (%d loose, %d asset(s))", folder, len(items), len(asset_ids))
        entries = _activity_index(snipe, asset_ids) if asset_ids else []

        for f in sorted(items, key=lambda x: x["name"]):
            m = WITH_UPLOADER_RE.match(f["name"])
            if not m:
                if NO_UPLOADER_RE.match(f["name"]):
                    no_event.append(f)
                    log.info("    %-52s  no uploader, not from an event", f["name"])
                else:
                    log.info("    %-52s  unrecognized name, skipping", f["name"])
                continue

            entry = _find_entry(entries, m.group("uploader"), m.group("stamp"))
            if not entry:
                unmatched.append(f)
                log.info("    %-52s  no matching activity entry", f["name"])
                continue

            sub = _event_subfolder_name(
                entry.get("action_type") or "",
                _extract_uploader_name(entry),
                _extract_entry_date(entry),
            )
            moves.append((f, f"{folder}/{safe_name(sub)}"))
            log.info("    %-52s  -> %s", f["name"], safe_name(sub))

    # Files stamped with a sync run time (from the old parse_dt bug) can't be
    # matched by name. Check whether the same photo already sits in an event
    # subfolder of the same model folder — the duplicate cleanup never
    # compared across folder boundaries, so these were never examined.
    already_filed: List[tuple] = []
    if unmatched:
        log.info("")
        log.info("--- checking unmatched files against event subfolders " + "-" * 15)
        nested_by_model: Dict[str, List[dict]] = defaultdict(list)
        for f in files:
            rel = f["path"][len(base):].strip("/")
            parts = rel.split("/")
            if len(parts) > 3:
                nested_by_model["/".join([base] + parts[:2])].append(f)

        cache: Dict[str, Optional[str]] = {}

        def ph(item: dict) -> Optional[str]:
            if item["id"] not in cache:
                data = drive.download_item(item["id"])
                cache[item["id"]] = perceptual_hash(data) if data else None
            return cache[item["id"]]

        still_orphan: List[dict] = []
        for f in unmatched:
            model_folder = f["path"].rsplit("/", 1)[0]
            mine = ph(f)
            if not mine:
                still_orphan.append(f)
                continue
            target = int(mine, 16)
            hit = None
            for cand in nested_by_model.get(model_folder, []):
                other = ph(cand)
                if not other:
                    continue
                if bin(target ^ int(other, 16)).count("1") <= PERCEPTUAL_MAX_DISTANCE:
                    hit = cand
                    break
            if hit:
                already_filed.append((f, hit))
                log.info("    %-52s  already in %s",
                         f["name"], hit["path"].rsplit("/", 2)[-2])
            else:
                still_orphan.append(f)
                log.info("    %-52s  not found in any event folder", f["name"])
        unmatched = still_orphan

    log.info("")
    log.info("=" * 70)
    log.info("%d file(s) can be filed into an event subfolder", len(moves))
    log.info("%d file(s) are already filed in an event subfolder (redundant copy)",
             len(already_filed))
    log.info("%d file(s) have no event (asset image / notes) — leaving in place", len(no_event))
    log.info("%d file(s) matched nothing at all — leaving in place", len(unmatched))

    if not args.apply:
        log.info("")
        log.info("Dry run — nothing moved. Re-run with --apply to perform the moves.")
        return 0

    outside = [f for f, _ in moves if not f["path"].startswith(f"{base}/")]
    if outside:
        log.error("Refusing to move — %d file(s) outside %s", len(outside), base)
        return 1

    folder_ids: Dict[str, str] = {}
    moved = 0
    for f, target in moves:
        try:
            if target not in folder_ids:
                folder_ids[target] = drive.ensure_folder(target)["id"]
            drive.move_item(f["id"], folder_ids[target])
            moved += 1
        except Exception as exc:
            log.error("Failed to move %s: %s", f["path"], exc)

    log.info("")
    log.info("Moved %d of %d file(s).", moved, len(moves))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
