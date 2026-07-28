"""AC Unit checkout notifier.

Polls the Snipe-IT activity log for checkouts of assets in the configured
category (default: "AC Units") and sends an email via Microsoft Graph API
for each new checkout.

Run:
    python notifications/notifier.py   # from repo root
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import msal
import requests
from dotenv import load_dotenv

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]


# ------------------------------------------------------------------ #
# Config
# ------------------------------------------------------------------ #
@dataclass
class Config:
    snipe_url: str
    snipe_token: str
    azure_tenant: str
    azure_client_id: str
    azure_client_secret: str
    notify_from: str
    notify_to: List[str]
    category_name: str
    state_path: str


def _require(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


def load_config() -> Config:
    load_dotenv()
    return Config(
        snipe_url=_require("SNIPE_URL").rstrip("/"),
        snipe_token=_require("SNIPE_API_KEY"),
        azure_tenant=_require("AZURE_TENANT_ID"),
        azure_client_id=_require("AZURE_CLIENT_ID"),
        azure_client_secret=_require("AZURE_CLIENT_SECRET"),
        notify_from=_require("NOTIFY_FROM_EMAIL"),
        notify_to=[
            e.strip()
            for e in _require("NOTIFY_TO_EMAILS").split(",")
            if e.strip()
        ],
        category_name=os.environ.get("NOTIFY_CATEGORY", "AC Units").strip() or "AC Units",
        state_path=os.environ.get("NOTIFY_STATE_PATH", "ac_notify_state.json").strip()
        or "ac_notify_state.json",
    )


# ------------------------------------------------------------------ #
# State — tracks which activity log entry IDs we've already notified on
# ------------------------------------------------------------------ #
@dataclass
class NotifyState:
    seen_ids: Set[int] = field(default_factory=set)
    # False when no state file was found. The Actions cache can be evicted or
    # miss, and treating that as "everything is new" would email a
    # notification for every recent checkout at once.
    loaded_from_disk: bool = False

    @classmethod
    def load(cls, path: str) -> "NotifyState":
        try:
            data = json.loads(Path(path).read_text())
            return cls(seen_ids=set(data.get("seen_ids", [])), loaded_from_disk=True)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()

    def save(self, path: str) -> None:
        ids = sorted(self.seen_ids)[-10_000:]
        Path(path).write_text(json.dumps({"seen_ids": ids}, indent=2))


# ------------------------------------------------------------------ #
# Snipe-IT client
# ------------------------------------------------------------------ #
class SnipeClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        r = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=30
        )
        r.raise_for_status()
        return r.json()

    def get_category_id(self, name: str) -> Optional[int]:
        data = self._get("/api/v1/categories", {"search": name, "limit": 50})
        for row in data.get("rows") or []:
            if (row.get("name") or "").strip().lower() == name.lower():
                return row["id"]
        return None

    def get_asset_ids_in_category(self, category_id: int) -> Set[int]:
        ids: Set[int] = set()
        offset = 0
        while True:
            data = self._get(
                "/api/v1/hardware",
                {
                    "category_id": category_id, "limit": 500, "offset": offset,
                    # Unsorted offset paging can repeat one row and skip
                    # another, which would drop an AC unit from the watch list.
                    "sort": "id", "order": "asc",
                },
            )
            rows = data.get("rows") or []
            for row in rows:
                ids.add(row["id"])
            offset += len(rows)
            if not rows or offset >= (data.get("total") or 0):
                break
        return ids

    def get_asset(self, asset_id: int) -> dict:
        return self._get(f"/api/v1/hardware/{asset_id}")

    def get_recent_checkouts(self, limit: int = 200) -> List[dict]:
        data = self._get(
            "/api/v1/reports/activity",
            {
                "action_type": "checkout",
                "item_type": "asset",
                "limit": limit,
                "sort": "created_at",
                "order": "desc",
            },
        )
        return data.get("rows") or []

    def get_asset_history(self, asset_id: int, limit: int = 25) -> List[dict]:
        data = self._get(
            "/api/v1/reports/activity",
            {
                "item_type": "asset",
                "item_id": asset_id,
                "limit": limit,
                "sort": "created_at",
                "order": "desc",
            },
        )
        return data.get("rows") or []


# ------------------------------------------------------------------ #
# Graph mail sender
# ------------------------------------------------------------------ #
class GraphMailSender:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        from_address: str,
    ) -> None:
        self.from_address = from_address
        self._app = msal.ConfidentialClientApplication(
            client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )

    def _token(self) -> str:
        result = self._app.acquire_token_silent(GRAPH_SCOPE, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if not result or "access_token" not in result:
            raise RuntimeError(f"Failed to acquire Graph token: {result}")
        return result["access_token"]

    def send(self, to: List[str], subject: str, html_body: str) -> None:
        payload = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [
                    {"emailAddress": {"address": a}} for a in to
                ],
            },
            "saveToSentItems": False,
        }
        r = requests.post(
            f"{GRAPH_BASE}/users/{self.from_address}/sendMail",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        r.raise_for_status()


# ------------------------------------------------------------------ #
# Email builder
# ------------------------------------------------------------------ #
def _fmt_date(val: Any) -> str:
    if isinstance(val, dict):
        return val.get("formatted") or val.get("datetime") or ""
    return str(val) if val else ""


def _person_name(entry: dict, *keys: str) -> str:
    for key in keys:
        actor = entry.get(key)
        if isinstance(actor, dict):
            name = (actor.get("name") or "").strip()
            if not name:
                first = (actor.get("first_name") or "").strip()
                last = (actor.get("last_name") or "").strip()
                name = f"{first} {last}".strip()
            return name or actor.get("username") or ""
        if isinstance(actor, str) and actor.strip():
            return actor.strip()
    return ""


def _action_label(action_type: str) -> str:
    at = (action_type or "").lower()
    if "checkin" in at:
        return "Check In"
    if "checkout" in at:
        return "Check Out"
    if "update" in at:
        return "Update"
    return action_type or "—"


def build_email(
    asset: dict, checkout_entry: dict, history: List[dict], snipe_url: str
) -> tuple[str, str]:
    model = asset.get("model") or {}
    manufacturer = asset.get("manufacturer") or {}

    asset_tag = asset.get("asset_tag") or "—"
    model_name = (model.get("name") or "") if isinstance(model, dict) else ""
    model_number = (model.get("model_number") or "") if isinstance(model, dict) else ""
    manufacturer_name = (manufacturer.get("name") or "") if isinstance(manufacturer, dict) else ""
    asset_url = f"{snipe_url}/hardware/{asset.get('id')}"

    checked_out_to = _person_name(checkout_entry, "target")
    if not checked_out_to:
        target = checkout_entry.get("target") or {}
        checked_out_to = (target.get("name") or "") if isinstance(target, dict) else ""
    checked_out_by = _person_name(checkout_entry, "admin", "created_by", "user")
    checkout_date = _fmt_date(checkout_entry.get("created_at"))
    note = checkout_entry.get("note") or ""

    subject = f"AC Unit Checked Out: {asset_tag} — {model_name or 'Unknown Model'}"

    history_rows = ""
    for entry in history:
        action = _action_label(entry.get("action_type") or "")
        date = _fmt_date(entry.get("created_at"))
        target = entry.get("target") or {}
        target_name = (target.get("name") or "") if isinstance(target, dict) else str(target)
        by = _person_name(entry, "admin", "created_by", "user")
        entry_note = entry.get("note") or ""
        history_rows += f"""
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;white-space:nowrap;">{date}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;">{action}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;">{target_name}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;">{by}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;">{entry_note}</td>
        </tr>"""

    note_row = (
        f"<tr><td style='padding:8px 16px;font-weight:bold;background:#f9f9f9;'>Note</td>"
        f"<td style='padding:8px 16px;background:#f9f9f9;'>{note}</td></tr>"
        if note
        else ""
    )

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;color:#222;max-width:720px;margin:0 auto;padding:24px;">
  <h2 style="color:#1a56a0;margin-bottom:4px;">AC Unit Checked Out</h2>
  <p style="color:#888;margin-top:0;font-size:14px;">{checkout_date}</p>

  <table style="width:100%;border-collapse:collapse;margin-bottom:28px;border:1px solid #e0e0e0;border-radius:6px;overflow:hidden;">
    <tr><td style="padding:8px 16px;font-weight:bold;width:160px;">Asset Tag</td><td style="padding:8px 16px;">{asset_tag}</td></tr>
    <tr style="background:#f4f6fa;"><td style="padding:8px 16px;font-weight:bold;">Model Name</td><td style="padding:8px 16px;">{model_name or "—"}</td></tr>
    <tr><td style="padding:8px 16px;font-weight:bold;">Model Number</td><td style="padding:8px 16px;">{model_number or "—"}</td></tr>
    <tr style="background:#f4f6fa;"><td style="padding:8px 16px;font-weight:bold;">Manufacturer</td><td style="padding:8px 16px;">{manufacturer_name or "—"}</td></tr>
    <tr><td style="padding:8px 16px;font-weight:bold;">Checked Out To</td><td style="padding:8px 16px;">{checked_out_to or "—"}</td></tr>
    <tr style="background:#f4f6fa;"><td style="padding:8px 16px;font-weight:bold;">Checked Out By</td><td style="padding:8px 16px;">{checked_out_by or "—"}</td></tr>
    {note_row}
  </table>

  <h3 style="color:#1a56a0;margin-bottom:10px;">Checkout History</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px;border:1px solid #e0e0e0;">
    <thead>
      <tr style="background:#1a56a0;color:#fff;">
        <th style="padding:8px 12px;text-align:left;">Date</th>
        <th style="padding:8px 12px;text-align:left;">Action</th>
        <th style="padding:8px 12px;text-align:left;">User / Location</th>
        <th style="padding:8px 12px;text-align:left;">By</th>
        <th style="padding:8px 12px;text-align:left;">Note</th>
      </tr>
    </thead>
    <tbody>
      {history_rows or '<tr><td colspan="5" style="padding:10px 12px;color:#888;">No history found.</td></tr>'}
    </tbody>
  </table>

  <p style="margin-top:28px;font-size:12px;color:#aaa;">
    <a href="{asset_url}" style="color:#1a56a0;">View asset in Snipe-IT</a> &nbsp;·&nbsp;
    Sent by JD McCormick asset tracking
  </p>
</body>
</html>"""

    return subject, html


# ------------------------------------------------------------------ #
# Checkout processing
# ------------------------------------------------------------------ #
def process_checkouts(checkouts, ac_asset_ids, state, send) -> tuple:
    """Notify on unseen checkouts of watched assets. Returns (sent, failed).

    ``send(entry, asset_id)`` performs the notification and raises on failure.

    An entry is recorded as seen only once its notification has gone out, so a
    transient Graph or Snipe-IT failure retries on the next run rather than
    being dropped silently. Entries for assets we don't watch are recorded
    immediately since there is nothing to send.
    """
    sent = failed = 0
    for entry in checkouts:
        entry_id = entry.get("id")
        if not entry_id or entry_id in state.seen_ids:
            continue

        item = entry.get("item") or {}
        asset_id = item.get("id")

        if asset_id not in ac_asset_ids:
            state.seen_ids.add(entry_id)
            continue

        try:
            send(entry, asset_id)
        except Exception:
            failed += 1
            log.exception("Failed to notify for asset_id=%s — will retry", asset_id)
            continue

        state.seen_ids.add(entry_id)
        sent += 1
    return sent, failed


# ------------------------------------------------------------------ #
# Main
# ------------------------------------------------------------------ #
def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config()
    snipe = SnipeClient(cfg.snipe_url, cfg.snipe_token)
    mailer = GraphMailSender(
        cfg.azure_tenant, cfg.azure_client_id, cfg.azure_client_secret, cfg.notify_from
    )
    state = NotifyState.load(cfg.state_path)

    category_id = snipe.get_category_id(cfg.category_name)
    if not category_id:
        log.error("Category '%s' not found in Snipe-IT", cfg.category_name)
        return 1
    log.info("Category '%s' → id=%d", cfg.category_name, category_id)

    ac_asset_ids = snipe.get_asset_ids_in_category(category_id)
    log.info("%d assets in '%s'", len(ac_asset_ids), cfg.category_name)

    checkouts = snipe.get_recent_checkouts()
    log.info("%d recent checkout events fetched", len(checkouts))

    if not state.loaded_from_disk:
        state.seen_ids.update(e["id"] for e in checkouts if e.get("id"))
        state.save(cfg.state_path)
        log.warning(
            "No prior state file — recorded %d existing checkout(s) without "
            "notifying. Future checkouts will be emailed normally.",
            len(state.seen_ids),
        )
        return 0

    def send(entry, asset_id):
        log.info("New AC unit checkout — asset_id=%s entry_id=%s", asset_id, entry.get("id"))
        asset = snipe.get_asset(asset_id)
        history = snipe.get_asset_history(asset_id)
        subject, body = build_email(asset, entry, history, cfg.snipe_url)
        mailer.send(cfg.notify_to, subject, body)
        log.info("Notified %s — asset %s", cfg.notify_to, asset.get("asset_tag"))

    try:
        notified, failed = process_checkouts(checkouts, ac_asset_ids, state, send)
    finally:
        # Persist regardless, so sends that already went out are never repeated.
        state.save(cfg.state_path)

    log.info("Done. notified=%d failed=%d", notified, failed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
