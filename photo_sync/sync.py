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
from helpers.link_resolver import extract_urls, resolve_url
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
    notes = info["notes"]

    urls = extract_urls(notes)
    if not urls:
        return []

    log.info("Asset %s (%s): found %d URL(s)", asset_id, asset_tag, len(urls))

    safe_tag = safe_asset_tag(asset_tag)
    folder = drive.asset_folder(safe_tag)
    folder_ensured = False

    results: List[UploadResult] = []
    starting_count = store.count_for_asset(asset_id)

    for i, url in enumerate(urls, start=1):
        if not cfg.force_resync and store.is_processed(asset_id, url):
            log.debug("  [%d] %s — already processed, skipping", i, url)
            results.append(
                UploadResult(source_url=url, onedrive_url="", skipped_reason="already_uploaded")
            )
            continue

        log.info("  [%d] resolving %s", i, url)
        resolved = resolve_url(url)
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

        index = starting_count + len(
            [r for r in results if not r.skipped_reason]
        ) + 1
        filename = build_filename(safe_tag, index, ext)

        log.info("  [%d] uploading %s (%d bytes)", i, filename, len(content))
        try:
            file_id, web_url = drive.upload_small_file(
                folder, filename, content, content_type=mime
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

    totals = {"assets_seen": 0, "assets_with_urls": 0, "uploaded": 0, "skipped": 0, "failed": 0}

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
            for r in results:
                if r.onedrive_url and not r.skipped_reason:
                    totals["uploaded"] += 1
                elif r.skipped_reason in (None, "already_uploaded", "duplicate_hash"):
                    totals["skipped"] += 1
                else:
                    totals["failed"] += 1

        if args.limit and totals["assets_seen"] >= args.limit:
            log.info("Hit --limit %d, stopping", args.limit)
            break

    log.info(
        "Done. assets_seen=%d assets_with_urls=%d uploaded=%d skipped=%d failed=%d",
        totals["assets_seen"],
        totals["assets_with_urls"],
        totals["uploaded"],
        totals["skipped"],
        totals["failed"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
