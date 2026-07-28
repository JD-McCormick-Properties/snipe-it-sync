"""Setup for the notifier suite.

Nothing here touches the network or real credentials.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("SNIPE_URL", "https://snipe.invalid/")
os.environ.setdefault("SNIPE_API_KEY", "test-token")
os.environ.setdefault("AZURE_TENANT_ID", "tenant")
os.environ.setdefault("AZURE_CLIENT_ID", "client")
os.environ.setdefault("AZURE_CLIENT_SECRET", "secret")
os.environ.setdefault("NOTIFY_FROM_EMAIL", "info@example.com")
os.environ.setdefault("NOTIFY_TO_EMAILS", "a@example.com,b@example.com")

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


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
