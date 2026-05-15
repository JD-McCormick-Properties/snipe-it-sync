"""Microsoft Graph / OneDrive uploader using app-only (client credentials) auth.

App-only auth has no concept of "/me", so the caller MUST supply either
a target user (ONEDRIVE_USER_ID — usually a UPN like svc-asset@yourco.com)
or a drive id directly (ONEDRIVE_DRIVE_ID). User-id form is more common.

Files are placed at:
    {ONEDRIVE_BASE_FOLDER}/{asset_tag}/{filename}

The base folder defaults to "AssetPhotos".
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import re

import msal
import requests

_SAFE_NAME_RE = re.compile(r"[^\w\s._-]")


def _safe_folder(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("", name.strip())
    return re.sub(r"\s+", " ", cleaned).strip()

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPE = ["https://graph.microsoft.com/.default"]


class OneDriveClient:
    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        user_id: Optional[str] = None,
        drive_id: Optional[str] = None,
        base_folder: str = "AssetPhotos",
        timeout: int = 60,
    ) -> None:
        if not user_id and not drive_id:
            raise ValueError(
                "Either ONEDRIVE_USER_ID or ONEDRIVE_DRIVE_ID must be set"
            )
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = user_id
        self.drive_id = drive_id
        self.base_folder = base_folder.strip("/")
        self.timeout = timeout

        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )
        self._token: Optional[str] = None

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #
    def _get_token(self) -> str:
        result = self._app.acquire_token_silent(SCOPE, account=None)
        if not result:
            result = self._app.acquire_token_for_client(scopes=SCOPE)
        if not result or "access_token" not in result:
            raise RuntimeError(
                f"Failed to acquire Graph token: "
                f"{result.get('error_description') if result else 'no response'}"
            )
        self._token = result["access_token"]
        return self._token

    def _headers(self, content_type: Optional[str] = None) -> Dict[str, str]:
        token = self._token or self._get_token()
        h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if content_type:
            h["Content-Type"] = content_type
        return h

    # ------------------------------------------------------------------ #
    # URL builder
    # ------------------------------------------------------------------ #
    def _drive_root(self) -> str:
        if self.drive_id:
            return f"{GRAPH_BASE}/drives/{self.drive_id}/root"
        # user_id can be a UPN or an object id; both are URL-safe enough
        return f"{GRAPH_BASE}/users/{quote(self.user_id, safe='@')}/drive/root"

    def _path_url(self, path: str) -> str:
        """Return a Graph URL referencing a path under the drive root.

        Path is relative to the drive root (no leading slash).
        """
        encoded = quote(path.strip("/"), safe="/")
        return f"{self._drive_root()}:/{encoded}"

    # ------------------------------------------------------------------ #
    # Folder / file ops
    # ------------------------------------------------------------------ #
    def ensure_folder(self, folder_path: str) -> Dict[str, Any]:
        """Create folder_path under the drive root if it doesn't already exist.

        folder_path is a forward-slash separated path like "AssetPhotos/AT-001".
        """
        folder_path = folder_path.strip("/")
        if not folder_path:
            raise ValueError("folder_path cannot be empty")

        # Walk from root, creating segments as needed.
        segments = folder_path.split("/")
        current = ""
        last_resp: Dict[str, Any] = {}
        for seg in segments:
            parent = current
            current = f"{current}/{seg}" if current else seg

            # Try GET to see if it exists.
            url = f"{self._path_url(current)}"
            r = requests.get(url, headers=self._headers(), timeout=self.timeout)
            if r.status_code == 200:
                last_resp = r.json()
                continue
            if r.status_code != 404:
                r.raise_for_status()

            # Create under the parent.
            create_url = (
                f"{self._path_url(parent)}:/children"
                if parent
                else f"{self._drive_root()}/children"
            )
            payload = {
                "name": seg,
                "folder": {},
                # 'fail' is intentional: we only POST after a 404, so any
                # conflict here means our existence check disagreed with
                # reality and we'd rather see a loud error than risk
                # touching a pre-existing folder on a shared drive.
                "@microsoft.graph.conflictBehavior": "fail",
            }
            cr = requests.post(
                create_url,
                headers=self._headers("application/json"),
                json=payload,
                timeout=self.timeout,
            )
            cr.raise_for_status()
            last_resp = cr.json()
            log.info("Created OneDrive folder /%s", current)

        return last_resp

    def file_exists(self, folder_path: str, filename: str) -> bool:
        url = self._path_url(f"{folder_path.strip('/')}/{filename}")
        r = requests.get(url, headers=self._headers(), timeout=self.timeout)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
        r.raise_for_status()
        return False

    def upload_small_file(
        self,
        folder_path: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        description: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Upload up to ~4 MB via simple PUT. Returns (file_id, web_url).

        If ``description`` is provided, it's set on the resulting drive
        item via a follow-up PATCH (Graph doesn't allow setting metadata
        in the PUT-content call itself).

        For larger files, switch to upload sessions; current asset photos
        are well within this limit.
        """
        if len(content) > 4 * 1024 * 1024:
            return self._upload_large_file(
                folder_path, filename, content, content_type, description=description
            )

        path = f"{folder_path.strip('/')}/{filename}"
        url = f"{self._path_url(path)}:/content"
        r = requests.put(
            url,
            headers=self._headers(content_type),
            data=content,
            timeout=self.timeout,
        )
        r.raise_for_status()
        data = r.json()
        file_id = data.get("id", "")
        web_url = data.get("webUrl", "")

        if description and file_id:
            self._set_item_metadata(file_id, {"description": description})

        return file_id, web_url

    def _set_item_metadata(self, file_id: str, fields: Dict[str, Any]) -> None:
        """PATCH a driveItem to update fields like description."""
        if self.drive_id:
            url = f"{GRAPH_BASE}/drives/{self.drive_id}/items/{file_id}"
        else:
            url = (
                f"{GRAPH_BASE}/users/{quote(self.user_id, safe='@')}"
                f"/drive/items/{file_id}"
            )
        try:
            r = requests.patch(
                url,
                headers=self._headers("application/json"),
                json=fields,
                timeout=self.timeout,
            )
            r.raise_for_status()
        except Exception as exc:
            log.warning(
                "Failed to set metadata on driveItem %s: %s", file_id, exc
            )

    def _upload_large_file(
        self,
        folder_path: str,
        filename: str,
        content: bytes,
        content_type: str,
        description: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Upload via Graph upload session (chunked) for files >4 MB."""
        path = f"{folder_path.strip('/')}/{filename}"
        create_url = f"{self._path_url(path)}:/createUploadSession"
        sess = requests.post(
            create_url,
            headers=self._headers("application/json"),
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": "replace",
                    "name": filename,
                }
            },
            timeout=self.timeout,
        )
        sess.raise_for_status()
        upload_url = sess.json()["uploadUrl"]

        chunk_size = 5 * 1024 * 1024  # 5 MiB
        total = len(content)
        offset = 0
        last_json: Dict[str, Any] = {}

        while offset < total:
            end = min(offset + chunk_size, total) - 1
            headers = {
                "Content-Length": str(end - offset + 1),
                "Content-Range": f"bytes {offset}-{end}/{total}",
                "Content-Type": content_type,
            }
            chunk = content[offset : end + 1]
            r = requests.put(
                upload_url, headers=headers, data=chunk, timeout=self.timeout
            )
            if r.status_code in (200, 201):
                last_json = r.json()
            elif r.status_code != 202:
                r.raise_for_status()
            offset = end + 1

        file_id = last_json.get("id", "")
        web_url = last_json.get("webUrl", "")
        if description and file_id:
            self._set_item_metadata(file_id, {"description": description})
        return file_id, web_url

    def asset_folder(self, category: str, model: str) -> str:
        """Folder path for a given asset: {base}/{category}/{model}."""
        safe_cat = _safe_folder(category) or "Uncategorized"
        safe_mod = _safe_folder(model) or "Unknown Model"
        return f"{self.base_folder}/{safe_cat}/{safe_mod}"
