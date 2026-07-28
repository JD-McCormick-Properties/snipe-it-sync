"""Setup for the property sync suite.

Nothing here touches the network or real credentials. sync_properties reads
its config at import time, so the environment is populated before any test
module imports it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("SNIPE_URL", "https://snipe.invalid/")
os.environ.setdefault("SNIPE_API_KEY", "test-token")

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))


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
