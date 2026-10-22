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

Read-only; changes nothing. Run locally it prints the report. Under GitHub
Actions the run log is public, and this report is a per-person table, so it
is emailed to ALERT_TO_EMAILS instead and the log records only that it was
sent.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List

import requests

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from helpers import log_redaction
from helpers.link_resolver import extract_urls
from helpers.onedrive import GRAPH_BASE, OneDriveClient
from helpers.snipeit import SnipeITClient, summarize_asset
from sync import _configure_logging, _extract_entry_date, _extract_uploader_name, load_config

log = logging.getLogger("usage_report")

# "Check Out - Alex Rivera - 2026-08-10 08-38"
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
    lines: List[str] = []

    def out(fmt: str = "", *args) -> None:
        lines.append(fmt % args if args else fmt)

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

    out("%d file(s), %d in named event folders", len(files), len(files) - undated)
    out("")
    out("Photos in OneDrive, credited to whoever performed the check-in/check-out")
    out("(the event folder is named after them, which is not necessarily the")
    out("person who attached the photo — see the UPLOADS table below for that)")
    out("")
    out("%-20s %7s %7s %9s  %s", "PERSON", "PHOTOS", "EVENTS", "VEHICLES", "LAST")
    out("%s", "-" * 62)
    for name, p in sorted(per_person.items(), key=lambda kv: -kv[1]["photos"]):
        out("%-20s %7d %7d %9d  %s",
                 name[:20], p["photos"], len(p["events"]), len(p["vehicles"]), p["last"] or "?")
    if undated:
        out("%-20s %7d  (asset image / notes / unnamed folders)", "unattributed", undated)

    # ---------------- Snipe-IT: which route are they using? ------------- #
    out("")
    out("=" * 62)
    out("Route used, per person, from Snipe-IT (last 60 days)")
    out("")

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
    out("%-20s %10s %10s %10s", "PERSON", "EVENTS", "UPLOADS", "LINK EVTS")
    out("%s", "-" * 55)
    for who in people:
        out("%-20s %10d %10d %10d",
                 who[:20], checkouts.get(who, 0), native.get(who, 0), linked.get(who, 0))

    out("")
    out("EVENTS     check-ins AND check-outs in the last 60 days, combined")
    out("UPLOADS    'uploaded' activity entries — files attached via SnipeMobile,")
    out("           credited to the person Snipe-IT names as the uploader")
    out("LINK EVTS  events whose note held a share link. Counts events, not")
    out("           photos — one album link can be a dozen photos, so this is")
    out("           NOT comparable to the UPLOADS column")

    if log_redaction.should_redact():
        recipients = [e.strip() for e in os.environ.get("ALERT_TO_EMAILS", "").split(",") if e.strip()]
        sender = os.environ.get("NOTIFY_FROM_EMAIL", "").strip()
        if not recipients or not sender:
            log.error("Report not sent: ALERT_TO_EMAILS and NOTIFY_FROM_EMAIL must be set")
            return 1
        _email_report(drive, sender, recipients, "Photo usage report", lines)
        log.info("Report emailed to %d recipient(s); not printed because run logs are public",
                 len(recipients))
    else:
        for line in lines:
            log.info("%s", line)
    return 0


def _email_report(drive: OneDriveClient, sender: str, to: List[str], subject: str,
                  lines: List[str]) -> None:
    """Send the report as monospaced text through Microsoft Graph.

    Uses the same app registration as the uploads; it already holds Mail.Send
    for the AC unit notifier.
    """
    body = "<pre style=\"font-family:Consolas,Menlo,monospace;font-size:13px\">" \
        + html.escape("\n".join(lines)) + "</pre>"
    r = requests.post(
        f"{GRAPH_BASE}/users/{sender}/sendMail",
        headers=drive._headers("application/json"),
        json={
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": body},
                "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            },
            "saveToSentItems": False,
        },
        timeout=30,
    )
    r.raise_for_status()


if __name__ == "__main__":
    raise SystemExit(main())
