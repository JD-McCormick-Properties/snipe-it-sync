"""Fetching the AppFolio exports from OneDrive.

They used to be committed to this public repository. They now live in
OneDrive and are downloaded into data/ before each sync.
"""

from __future__ import annotations

from types import SimpleNamespace

import fetch_data

ROOT = "https://graph.microsoft.com/v1.0/users/svc@example.com/drive/root"
FOLDER = "JD McCormick Shared Drive/AppFolio Exports"


def _resp(status, content=b""):
    def raise_for_status():
        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
    return SimpleNamespace(status_code=status, content=content, raise_for_status=raise_for_status)


class FakeDrive:
    """Answers Graph calls from a {path: bytes} dict and records writes."""

    def __init__(self, files=None, existing_folders=()):
        self.files = dict(files or {})
        self.existing_folders = set(existing_folders)
        self.posts, self.puts = [], []

    def get(self, url, timeout):
        path = url[len(ROOT) + 2: -len(":/content")]
        return _resp(200, self.files[path]) if path in self.files else _resp(404)

    def put(self, url, data, timeout, headers):
        self.puts.append((url, data))
        return _resp(201)

    def post(self, url, json, timeout):
        self.posts.append((url, json["name"]))
        return _resp(409 if json["name"] in self.existing_folders else 201)


def test_downloads_both_exports_into_data(tmp_path):
    drive = FakeDrive({
        f"{FOLDER.replace(' ', '%20')}/properties.csv": b"p",
        f"{FOLDER.replace(' ', '%20')}/unit_directory.csv": b"u",
    })
    assert fetch_data.fetch(FOLDER, drive, ROOT, tmp_path) == 0
    assert (tmp_path / "properties.csv").read_bytes() == b"p"
    assert (tmp_path / "unit_directory.csv").read_bytes() == b"u"
    assert drive.puts == [] and drive.posts == []


def test_seeds_onedrive_from_a_local_copy_once(tmp_path):
    (tmp_path / "properties.csv").write_bytes(b"local-p")
    (tmp_path / "unit_directory.csv").write_bytes(b"local-u")
    drive = FakeDrive(existing_folders={"JD McCormick Shared Drive"})

    assert fetch_data.fetch(FOLDER, drive, ROOT, tmp_path) == 0

    assert [d for _, d in drive.puts] == [b"local-p", b"local-u"]
    assert drive.puts[0][0] == (
        f"{ROOT}:/JD%20McCormick%20Shared%20Drive/AppFolio%20Exports/properties.csv:/content"
    )
    # each folder level is created under the one before it; "already there" is fine
    assert drive.posts[:2] == [
        (f"{ROOT}/children", "JD McCormick Shared Drive"),
        (f"{ROOT}:/JD%20McCormick%20Shared%20Drive:/children", "AppFolio Exports"),
    ]


def test_missing_everywhere_fails_instead_of_syncing_nothing(tmp_path):
    assert fetch_data.fetch(FOLDER, FakeDrive(), ROOT, tmp_path) == 1


def test_drive_root_prefers_an_explicit_drive_id(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_DRIVE_ID", "b!abc")
    monkeypatch.setenv("ONEDRIVE_USER_ID", "svc@example.com")
    assert fetch_data._drive_root() == f"{fetch_data.GRAPH}/drives/b!abc/root"
    monkeypatch.delenv("ONEDRIVE_DRIVE_ID")
    assert fetch_data._drive_root() == ROOT
