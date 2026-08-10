"""Who is actually attaching photos, and by which route.

Two questions this answers that the run logs cannot:

  1. Who has photos in OneDrive at all, and when did they last add any.
     Read from the event folder names, which encode "<action> - <person> -
     <date>".

  2. Which route those photos took. A photo attached through SnipeMobile
     during a check-in/check-out becomes a *file on the asset* in Snipe-IT;
     a photo shared as a Google Photos link lives in the asset's notes or an
     activity note. OneDrive looks identical either way, so this has to come
     from Snipe-IT.

Read-only. Prints a report and changes nothing.
"""

from __future__ import annotations

import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from helpers.link_resolver import extract_urls
from helpers.onedrive import OneDriveClient
from helpers.snipeit import SnipeITClient, summarize_asset
from sync import _configure_logging, _extract_entry_date, _extract_uploader_name, load_config

log = logging.getLogger("usage_report")

# "Check Out - Nick Brown - 2026-08-10 08-38"
EVENT_FOLDER_RE = re.compile(
    r"^(?P<action>Check Out|Check In|Update|Added|Photos)"
    r"(?: - (?P<person>.+?))?"
    r"(?: - (?P<date>\d{4}-\d{2}-\d{2}) \d{2}-\d{2})?$"
)


def main() -> int:
    cfg = load_config()
    _configure_logging(cfg.log_level)

    drive = OneDriveClient(
        tenant_id=cfg.azure_tenant, client_id=cfg.azure_client_id,
        client_secret=cfg.azure_client_secret, user_id=cfg.onedrive_user_id,
        drive_id=cfg.onedrive_drive_id, base_folder=cfg.onedrive_base_folder,
    )
    snipe = SnipeITClient(cfg.snipe_url, cfg.snipe_token)
    base = cfg.onedrive_base_folder.strip("/")

    # ---------------- OneDrive: who has photos, and when ---------------- #
    log.info("Reading %s ...", base)
    files = drive.iter_files(base)

    per_person: Dict[str, dict] = defaultdict(
        lambda: {"photos": 0, "events": set(), "last": "", "vehicles": set()}
    )
    undated = 0
    for f in files:
        rel = f["path"][len(base):].strip("/").split("/")
        if len(rel) < 4:          # category/model/event/file
            undated += 1
            continue
        model, folder = rel[1], rel[2]
        m = EVENT_FOLDER_RE.match(folder)
        if not m or not m.group("person"):
            undated += 1
            continue
        p = per_person[m.group("person")]
        p["photos"] += 1
        p["events"].add(folder)
        p["vehicles"].add(model)
        if (m.group("date") or "") > p["last"]:
            p["last"] = m.group("date") or ""

    log.info("%d file(s), %d in named event folders", len(files), len(files) - undated)
    log.info("")
    log.info("%-20s %7s %7s %9s  %s", "PERSON", "PHOTOS", "EVENTS", "VEHICLES", "LAST")
    log.info("%s", "-" * 62)
    for name, p in sorted(per_person.items(), key=lambda kv: -kv[1]["photos"]):
        log.info("%-20s %7d %7d %9d  %s",
                 name[:20], p["photos"], len(p["events"]), len(p["vehicles"]), p["last"] or "?")
    if undated:
        log.info("%-20s %7d  (asset image / notes / unnamed folders)", "unattributed", undated)

    # ---------------- Snipe-IT: which route are they using? ------------- #
    log.info("")
    log.info("=" * 62)
    log.info("Route used, per person, from Snipe-IT (last 60 days)")
    log.info("")

    cutoff = datetime.now(timezone.utc) - timedelta(days=60)
    native: Dict[str, int] = defaultdict(int)   # photos attached via SnipeMobile
    linked: Dict[str, int] = defaultdict(int)   # photos shared as links in notes
    checkouts: Dict[str, int] = defaultdict(int)

    for asset in snipe.iter_hardware():
        info = summarize_asset(asset)
        aid = info["id"]

        try:
            entries_all = list(snipe.iter_asset_activity(aid))
        except Exception:
            continue
        entries = entries_all

        for e in entries:
            action = (e.get("action_type") or "").lower()
            if "checkin" not in action and "checkout" not in action:
                continue
            raw = _extract_entry_date(e)
            if not raw:
                continue
            try:
                when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if when < cutoff:
                continue
            who = _extract_uploader_name(e) or "unknown"
            checkouts[who] += 1
            if extract_urls(e.get("note") or e.get("notes") or ""):
                linked[who] += 1

        # Snipe-IT records its own "uploaded" activity entry naming whoever
        # attached the file. That is the real attribution — guessing from the
        # nearest check-in/check-out gets it wrong whenever two people have
        # attached files to the same vehicle.
        for e in entries_all:
            if "upload" not in (e.get("action_type") or "").lower():
                continue
            raw = _extract_entry_date(e)
            if raw:
                try:
                    when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=timezone.utc)
                    if when < cutoff:
                        continue
                except ValueError:
                    pass
            native[_extract_uploader_name(e) or "unknown"] += 1

    people = sorted(set(checkouts) | set(native) | set(linked))
    log.info("%-20s %10s %10s %10s", "PERSON", "CHECKOUTS", "SNIPEMOBILE", "LINKS")
    log.info("%s", "-" * 55)
    for who in people:
        log.info("%-20s %10d %10d %10d",
                 who[:20], checkouts.get(who, 0), native.get(who, 0), linked.get(who, 0))

    log.info("")
    log.info("CHECKOUTS   check-in/check-out events in the last 60 days")
    log.info("SNIPEMOBILE 'uploaded' activity entries — files attached to the asset")
    log.info("LINKS       events whose note contained a photo share link")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
