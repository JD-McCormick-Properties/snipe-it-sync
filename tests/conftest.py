"""Shared setup for the property-sync and notifier suites.

Nothing here touches the network or real credentials. sync_properties reads
its config at import time, so the environment is populated before any test
module imports it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("SNIPE_URL", "https://snipe.invalid/")
os.environ.setdefault("SNIPE_API_KEY", "test-token")
os.environ.setdefault("AZURE_TENANT_ID", "tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "client")
os.environ.setdefault("AZURE_CLIENT_SECRET", "secret")
os.environ.setdefault("NOTIFY_FROM_EMAIL", "info@example.com")
os.environ.setdefault("NOTIFY_TO_EMAILS", "a@example.com,b@example.com")

for path in (REPO_ROOT, REPO_ROOT / "notifications"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class FakeResponse:
    """Stands in for a requests.Response for the write-result checks."""

    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x" if payload is not None else b""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def checkout(entry_id: int, asset_id: int) -> dict:
    """An activity-log checkout entry shaped like Snipe-IT's response."""
    return {
        "id": entry_id,
        "action_type": "checkout",
        "item": {"id": asset_id, "name": f"asset-{asset_id}"},
        "target": {"name": "Nick Brown"},
        "admin": {"name": "Jill Weigen"},
        "created_at": {"datetime": "2026-07-27 09:00:00",
                       "formatted": "July 27, 2026 9:00am"},
    }
