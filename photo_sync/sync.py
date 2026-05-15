"""Snipe-IT photo sync entry point.

Reads asset notes from Snipe-IT, extracts URLs, resolves each to image
bytes (Google Photos / iCloud / direct), uploads them to OneDrive under
{ONEDRIVE_BASE_FOLDER}/{asset_tag}/, dedupes by (asset, url) and content
hash, and (optionally) writes the OneDrive web URLs back into the notes
field.

Run:
    python -m photo_sync.sync           # from the repo root
    python sync.py                      # from inside photo_sync/
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv

# Allow `python sync.py` from inside the photo_sync/ folder.
THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from helpers.dedupe import DedupeStore
from helpers.image_utils import (
    build_filename,
    hash_bytes,
    normalize_image,
    safe_asset_tag,
)
from helpers.link_resolver import extract_urls, is_icloud_share, resolve_url
from helpers.onedrive import OneDriveClient
from helpers.snipeit import SnipeITClient, summarize_asset


# ---------------------------------------------------------------------- #
# Logging
# ---------------------------------------------------------------------- #
def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Silence chatty libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("msal").setLevel(logging.WARNING)


log = logging.getLogger("photo_sync")


# ---------------------------------------------------------------------- #
# Config
# ---------------------------------------------------------------------- #
@dataclass
class Config:
    snipe_url: str
    snipe_token: str
    azure_tenant: str
    azure_client_id: str
    azure_client_secret: str
    onedrive_user_id: Optional[str]
    onedrive_drive_id: Optional[str]
    onedrive_base_folder: str
    write_back: bool
    force_resync: bool
    include_history_notes: bool
    use_playwright: bool
    db_path: str
    log_level: str


def _require(env: str) -> str:
    val = os.environ.get(env, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {env}")
    return val


def load_config() -> Config:
    load_dotenv()

    return Config(
        snipe_url=_require("SNIPE_URL").rstrip("/"),
        snipe_token=_require("SNIPE_API_KEY"),
        azure_tenant=_require("AZURE_TENANT_ID"),
        azure_client_id=_require("AZURE_CLIENT_ID"),
        azure_client_secret=_require("AZURE_CLIENT_SECRET"),
        onedrive_user_id=os.environ.get("ONEDRIVE_USER_ID", "").strip() or None,
        onedrive_drive_id=os.environ.get("ONEDRIVE_DRIVE_ID", "").strip() or None,
        onedrive_base_folder=os.environ.get(
            "ONEDRIVE_BASE_FOLDER", "AssetPhotos"
        ).strip()
        or "AssetPhotos",
        write_back=_truthy(os.environ.get("WRITE_BACK_TO_SNIPEIT", "false")),
        force_resync=_truthy(os.environ.get("FORCE_RESYNC", "false")),
        include_history_notes=_truthy(
            os.environ.get("INCLUDE_HISTORY_NOTES", "true")
        ),
        use_playwright=_truthy(os.environ.get("USE_PLAYWRIGHT", "true")),
        db_path=os.environ.get("DEDUPE_DB_PATH", "photo_sync_state.db").strip()
        or "photo_sync_state.db",
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO",
    )


def _truthy(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------- #
# Per-asset processing
# ---------------------------------------------------------------------- #
@dataclass
class UploadResult:
    source_url: str
    onedrive_url: str
    skipped_reason: Optional[str] = None


def process_asset(
    asset: dict,
    *,
    cfg: Config,
    snipe: SnipeITClient,
    drive: OneDriveClient,
    store: DedupeStore,
) -> List[UploadResult]:
    info = summarize_asset(asset)
    asset_id = info["id"]
    asset_tag = info["asset_tag"] or f"id-{asset_id}"
    asset_name = info["name"] or asset_tag
    notes = info["notes"]

    urls = extract_urls(notes)
    # Per-URL provenance: who/when, captured so we can write a useful
    # description into OneDrive. URLs from the top-level notes field
    # have no specific uploader.
    url_sources: dict = {u: {"uploader": "", "date": ""} for u in urls}
    history_url_count = 0

    # Asset image field — techs update this in the app when checking in/out.
    # Each new upload gets a new URL on the Snipe-IT server, so the content-
    # hash dedupe naturally accumulates one OneDrive file per unique photo.
    asset_image_url = info.get("image", "")
    if asset_image_url and asset_image_url not in url_sources:
        urls.insert(0, asset_image_url)
        url_sources[asset_image_url] = {"uploader": "", "date": ""}

    # Many techs paste photo links into individual check-in / check-out
    # notes (the asset's "history" tab) rather than the top-level notes
    # field. Pull those in too if the flag is on.
    if cfg.include_history_notes:
        seen = set(urls)
        try:
            for entry in snipe.iter_asset_activity(asset_id):
                entry_note = entry.get("note") or entry.get("notes") or ""
                if not entry_note:
                    continue
                uploader = _extract_uploader_name(entry)
                entry_date = _extract_entry_date(entry)
                for u in extract_urls(entry_note):
                    if u not in seen:
                        seen.add(u)
                        urls.append(u)
                        url_sources[u] = {
                            "uploader": uploader,
                            "date": entry_date,
                        }
                        history_url_count += 1
        except Exception as exc:
            log.warning(
                "Could not fetch activity log for asset %s: %s", asset_id, exc
            )

    if not urls:
        return []

    if history_url_count:
        log.info(
            "Asset %s (%s): %d URL(s) (%d from history notes)",
            asset_id,
            asset_tag,
            len(urls),
            history_url_count,
        )
    else:
        log.info(
            "Asset %s (%s): found %d URL(s)", asset_id, asset_tag, len(urls)
        )

    safe_tag = safe_asset_tag(asset_tag)
    folder = drive.asset_folder(info["category_name"], info["model_name"])
    folder_ensured = False

    results: List[UploadResult] = []

    for i, url in enumerate(urls, start=1):
        if not cfg.force_resync and store.is_processed(asset_id, url):
            log.debug("  [%d] %s — already processed, skipping", i, url)
            results.append(
                UploadResult(source_url=url, onedrive_url="", skipped_reason="already_uploaded")
            )
            continue

        # iCloud shares cannot be resolved automatically — Apple blocks
        # headless browsers, and the newer iCloud Link format is not
        # accessible via the sharedstreams API.  Flag for manual export.
        if is_icloud_share(url):
            log.warning(
                "  [%d] MANUAL ACTION REQUIRED — iCloud share cannot be "
                "processed automatically: %s",
                i,
                url,
            )
            results.append(
                UploadResult(
                    source_url=url,
                    onedrive_url="",
                    skipped_reason="icloud_manual",
                )
            )
            continue

        log.info("  [%d] resolving %s", i, url)
        resolved = resolve_url(url, use_playwright=cfg.use_playwright)
        if not resolved:
            log.warning("  [%d] could not resolve %s", i, url)
            results.append(
                UploadResult(source_url=url, onedrive_url="", skipped_reason="unresolved")
            )
            continue

        # Normalize HEIC -> JPG
        content, mime, ext = normalize_image(
            resolved.content, resolved.mime_type, resolved.extension
        )
        digest = hash_bytes(content)

        if not cfg.force_resync and store.has_hash_for_asset(asset_id, digest):
            log.info(
                "  [%d] hash %s already uploaded for asset %s — skipping",
                i,
                digest[:10],
                asset_id,
            )
            results.append(
                UploadResult(source_url=url, onedrive_url="", skipped_reason="duplicate_hash")
            )
            continue

        if not folder_ensured:
            drive.ensure_folder(folder)
            folder_ensured = True

        uploader = url_sources.get(url, {}).get("uploader", "")
        entry_date = url_sources.get(url, {}).get("date", "")
        filename = build_filename(info["model_name"], uploader, entry_date, ext)

        description = _build_file_description(
            asset_name=asset_name,
            asset_tag=asset_tag,
            source_url=url,
            uploader=url_sources.get(url, {}).get("uploader", ""),
            entry_date=url_sources.get(url, {}).get("date", ""),
        )

        log.info("  [%d] uploading %s (%d bytes)", i, filename, len(content))
        try:
            file_id, web_url = drive.upload_small_file(
                folder,
                filename,
                content,
                content_type=mime,
                description=description,
            )
        except Exception as exc:
            log.exception("  [%d] upload failed for %s: %s", i, url, exc)
            results.append(
                UploadResult(source_url=url, onedrive_url="", skipped_reason="upload_failed")
            )
            continue

        store.record_upload(
            asset_id=asset_id,
            asset_tag=safe_tag,
            source_url=url,
            content_hash=digest,
            onedrive_file_id=file_id,
            onedrive_url=web_url,
            filename=filename,
        )
        results.append(UploadResult(source_url=url, onedrive_url=web_url))
        log.info("  [%d] uploaded → %s", i, web_url)

    if cfg.write_back:
        new_notes = _append_writeback(notes, results)
        if new_notes != notes:
            try:
                snipe.update_notes(asset_id, new_notes)
                log.info("Wrote OneDrive links back to asset %s notes", asset_id)
            except Exception as exc:
                log.warning("Notes writeback failed for asset %s: %s", asset_id, exc)

    return results


def _extract_uploader_name(entry: dict) -> str:
    """Pull a human-friendly uploader name from a Snipe-IT activity entry.

    Activity entries put the actor under varying field names depending on
    the Snipe-IT version: ``admin``, ``created_by``, or ``user``. Each can
    be a dict with name fields or just a string.
    """
    for key in ("admin", "created_by", "user"):
        actor = entry.get(key)
        if isinstance(actor, dict):
            return (
                actor.get("name")
                or " ".join(
                    p
                    for p in (actor.get("first_name"), actor.get("last_name"))
                    if p
                ).strip()
                or actor.get("username")
                or ""
            )
        if isinstance(actor, str) and actor.strip():
            return actor.strip()
    return ""


def _extract_entry_date(entry: dict) -> str:
    """Best-effort human-readable date for a Snipe-IT activity entry."""
    for key in ("created_at", "action_date", "updated_at"):
        val = entry.get(key)
        if isinstance(val, dict):
            return (val.get("formatted") or val.get("datetime") or "").strip()
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _build_file_description(
    *,
    asset_name: str,
    asset_tag: str,
    source_url: str,
    uploader: str,
    entry_date: str,
) -> str:
    """Compose the description we attach to an uploaded OneDrive file.

    Format (parts dropped if empty):
      Asset: {name} ({tag}) | Uploaded by: {user} | Date: {date} | Source: {url}
    """
    parts: List[str] = []
    if asset_name and asset_tag and asset_name != asset_tag:
        parts.append(f"Asset: {asset_name} ({asset_tag})")
    elif asset_tag:
        parts.append(f"Asset: {asset_tag}")
    elif asset_name:
        parts.append(f"Asset: {asset_name}")
    if uploader:
        parts.append(f"Uploaded by: {uploader}")
    if entry_date:
        parts.append(f"Date: {entry_date}")
    if source_url:
        parts.append(f"Source: {source_url}")
    # OneDrive caps description at 1024 chars; keep some headroom.
    desc = " | ".join(parts)
    return desc[:1000]


def _append_writeback(notes: str, results: List[UploadResult]) -> str:
    """Append a 'OneDrive Backup:' section listing successful uploads.

    Idempotent — if the section is already in notes, replace it.
    """
    successful = [r for r in results if r.onedrive_url and not r.skipped_reason]
    if not successful:
        return notes

    marker = "OneDrive Backup:"
    body_lines = [marker] + [f"- {r.onedrive_url}" for r in successful]
    block = "\n".join(body_lines)

    if marker in notes:
        # Replace from marker to end of its block (until two newlines or EOF)
        idx = notes.find(marker)
        before = notes[:idx].rstrip()
        return f"{before}\n\n{block}".strip()

    base = notes.rstrip()
    if base:
        return f"{base}\n\n{block}"
    return block


# ---------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Snipe-IT → OneDrive photo sync")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-upload even if URL/hash is already in dedupe DB",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after processing this many assets (0 = no limit)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve URLs but don't upload to OneDrive or write to Snipe-IT",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.force:
        cfg.force_resync = True

    _configure_logging(cfg.log_level)
    log.info("Starting photo sync (force=%s, dry_run=%s)", cfg.force_resync, args.dry_run)

    snipe = SnipeITClient(cfg.snipe_url, cfg.snipe_token)
    drive = OneDriveClient(
        tenant_id=cfg.azure_tenant,
        client_id=cfg.azure_client_id,
        client_secret=cfg.azure_client_secret,
        user_id=cfg.onedrive_user_id,
        drive_id=cfg.onedrive_drive_id,
        base_folder=cfg.onedrive_base_folder,
    )
    store = DedupeStore(cfg.db_path)

    totals = {
        "assets_seen": 0,
        "assets_with_urls": 0,
        "uploaded": 0,
        "skipped": 0,
        "failed": 0,
        "icloud_manual": 0,
    }
    # Collect (asset_tag, url) pairs that need the manual iCloud export
    # workaround so we can print a single grouped summary at the end.
    icloud_pending: List[tuple] = []

    for asset in snipe.iter_hardware():
        totals["assets_seen"] += 1

        if args.dry_run:
            urls = extract_urls(asset.get("notes") or "")
            if urls:
                totals["assets_with_urls"] += 1
                log.info(
                    "DRY-RUN asset %s (%s): %d URL(s)",
                    asset.get("id"),
                    asset.get("asset_tag"),
                    len(urls),
                )
        else:
            results = process_asset(
                asset, cfg=cfg, snipe=snipe, drive=drive, store=store
            )
            if results:
                totals["assets_with_urls"] += 1
            asset_tag_label = (
                asset.get("asset_tag") or f"id-{asset.get('id', '?')}"
            )
            for r in results:
                if r.onedrive_url and not r.skipped_reason:
                    totals["uploaded"] += 1
                elif r.skipped_reason == "icloud_manual":
                    totals["icloud_manual"] += 1
                    icloud_pending.append((asset_tag_label, r.source_url))
                elif r.skipped_reason in (None, "already_uploaded", "duplicate_hash"):
                    totals["skipped"] += 1
                else:
                    totals["failed"] += 1

        if args.limit and totals["assets_seen"] >= args.limit:
            log.info("Hit --limit %d, stopping", args.limit)
            break

    log.info(
        "Done. assets_seen=%d assets_with_urls=%d uploaded=%d skipped=%d "
        "failed=%d icloud_manual=%d",
        totals["assets_seen"],
        totals["assets_with_urls"],
        totals["uploaded"],
        totals["skipped"],
        totals["failed"],
        totals["icloud_manual"],
    )

    if icloud_pending:
        log.warning(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
        log.warning(
            "MANUAL ACTION REQUIRED — %d iCloud URL(s) could not be "
            "processed automatically.",
            len(icloud_pending),
        )
        log.warning(
            "Use the iCloud web workaround (see photo_sync/README.md, "
            "'Shared link resolution' section) to export these photos "
            "manually and upload them to OneDrive."
        )
        log.warning("Affected assets:")
        for asset_tag, url in icloud_pending:
            log.warning("  [%s]  %s", asset_tag, url)
        log.warning(
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
