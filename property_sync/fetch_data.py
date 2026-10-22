"""Download the AppFolio exports from OneDrive into data/ before a sync.

The exports carry addresses, rents, deposits and revenue, and this repository
is public, so they live in OneDrive rather than in git. The sync itself still
reads data/properties.csv and data/unit_directory.csv; this script puts them
there. To refresh the data, replace the two files in the OneDrive folder.

Uses the same Azure app registration and target drive as the photo sync
(Files.ReadWrite.All, application permission).

If a file is missing from OneDrive but present locally, the local copy is
uploaded first. That is how the data moved out of the repository: the first
run after this script shipped seeded the folder from the committed copies.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import quote

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
DATA_DIR = Path(__file__).resolve().parent / "data"
FILES = ("properties.csv", "unit_directory.csv")
TIMEOUT = 60


def _token() -> str:
    app = msal.ConfidentialClientApplication(
        client_id=os.environ["AZURE_CLIENT_ID"],
        client_credential=os.environ["AZURE_CLIENT_SECRET"],
        authority=f"https://login.microsoftonline.com/{os.environ['AZURE_TENANT_ID']}",
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise RuntimeError(f"Graph token: {result.get('error_description') or result}")
    return result["access_token"]


def _drive_root() -> str:
    drive_id = os.environ.get("ONEDRIVE_DRIVE_ID", "").strip()
    if drive_id:
        return f"{GRAPH}/drives/{drive_id}/root"
    user = os.environ.get("ONEDRIVE_USER_ID", "").strip()
    if not user:
        raise RuntimeError("Set ONEDRIVE_USER_ID or ONEDRIVE_DRIVE_ID")
    return f"{GRAPH}/users/{quote(user, safe='@')}/drive/root"


def _ensure_folder(session: requests.Session, root: str, folder: str) -> None:
    """Create each segment of folder under the drive root if it is missing."""
    built: list = []
    for segment in folder.split("/"):
        parent = f"{root}:/{quote('/'.join(built))}:" if built else root
        r = session.post(
            f"{parent}/children",
            json={"name": segment, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            timeout=TIMEOUT,
        )
        if r.status_code not in (201, 409):  # 409: already there
            r.raise_for_status()
        built.append(segment)


def fetch(folder: str, session: requests.Session, root: str, data_dir: Path = DATA_DIR) -> int:
    """Download each export; seed OneDrive from a local copy if it is absent."""
    data_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        url = f"{root}:/{quote(folder)}/{quote(name)}:/content"
        local = data_dir / name
        r = session.get(url, timeout=TIMEOUT)
        if r.status_code == 404:
            if not local.exists():
                print(f"❌ {name} is not in OneDrive and there is no local copy")
                return 1
            _ensure_folder(session, root, folder)
            up = session.put(url, data=local.read_bytes(), timeout=TIMEOUT,
                             headers={"Content-Type": "text/csv"})
            up.raise_for_status()
            print(f"⬆️  {name}: not in OneDrive yet — uploaded the local copy ({local.stat().st_size} bytes)")
            continue
        r.raise_for_status()
        local.write_bytes(r.content)
        print(f"⬇️  {name}: {len(r.content)} bytes")
    return 0


def main() -> int:
    folder = os.environ.get("PROPERTY_DATA_FOLDER", "AppFolio Exports").strip().strip("/")
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {_token()}"
    return fetch(folder, session, _drive_root())


if __name__ == "__main__":
    raise SystemExit(main())
