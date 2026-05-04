"""Snipe-IT REST API client.

Handles authentication, paginated asset retrieval, and notes-field updates.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

log = logging.getLogger(__name__)


class SnipeITClient:
    """Thin wrapper around the Snipe-IT v1 REST API."""

    def __init__(self, base_url: str, api_token: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------ #
    # Internal request helper with simple retry on 429/5xx
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    log.warning(
                        "Snipe-IT %s %s returned %s (attempt %d/%d)",
                        method,
                        path,
                        resp.status_code,
                        attempt,
                        max_retries,
                    )
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                return resp.json() if resp.content else {}
            except requests.RequestException as exc:
                last_exc = exc
                log.warning(
                    "Snipe-IT request failed: %s (attempt %d/%d)",
                    exc,
                    attempt,
                    max_retries,
                )
                time.sleep(2 ** attempt)

        raise RuntimeError(
            f"Snipe-IT request failed after {max_retries} retries: {last_exc}"
        )

    # ------------------------------------------------------------------ #
    # Public methods
    # ------------------------------------------------------------------ #
    def iter_hardware(self, page_size: int = 100) -> Iterator[Dict[str, Any]]:
        """Yield every hardware asset by paginating /api/v1/hardware.

        Yields raw asset dicts as returned by Snipe-IT.
        """
        offset = 0
        total: Optional[int] = None

        while True:
            data = self._request(
                "GET",
                "/api/v1/hardware",
                params={"limit": page_size, "offset": offset},
            )
            rows: List[Dict[str, Any]] = data.get("rows", []) or []
            if total is None:
                total = data.get("total")
                log.info("Snipe-IT reports %s total hardware assets", total)

            if not rows:
                break

            for row in rows:
                yield row

            offset += len(rows)

            if total is not None and offset >= total:
                break

            # Safety stop if API stops paginating but didn't report total
            if len(rows) < page_size:
                break

    def get_asset(self, asset_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/api/v1/hardware/{asset_id}")

    def update_notes(self, asset_id: int, new_notes: str) -> Dict[str, Any]:
        """Patch the notes field on a hardware asset.

        Snipe-IT supports PATCH on /api/v1/hardware/{id}.
        """
        return self._request(
            "PATCH",
            f"/api/v1/hardware/{asset_id}",
            json={"notes": new_notes},
        )


def summarize_asset(asset: Dict[str, Any]) -> Dict[str, Any]:
    """Pull just the fields we care about from a Snipe-IT asset record."""
    category = asset.get("category") or {}
    return {
        "id": asset.get("id"),
        "asset_tag": (asset.get("asset_tag") or "").strip(),
        "name": (asset.get("name") or "").strip(),
        "serial": (asset.get("serial") or "").strip(),
        "notes": asset.get("notes") or "",
        "category_name": (category.get("name") if isinstance(category, dict) else "")
        or "",
    }
