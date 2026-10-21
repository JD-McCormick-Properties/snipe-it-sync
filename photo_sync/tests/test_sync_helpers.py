"""Foldering, event matching and filename selection in sync.py."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from conftest import activity, encode, make_photo

import sync
from helpers.dedupe import DedupeStore
from helpers.image_utils import hash_bytes
from sync import (
    ACTIVITY_MATCH_WINDOW,
    SKIPPED_REASONS,
    UNMATCHED_UPLOAD_FOLDER,
    UploadResult,
    _UrlBatch,
    _action_label,
    _append_writeback,
    _event_subfolder_name,
    _extract_entry_date,
    _extract_uploader_name,
    _native_upload_folder,
    _nearest_activity_entry,
    _process_batch,
    _subfolder_filename,
    process_asset,
)


# --------------------------------------------------------------------- #
# Snipe-IT writeback
# --------------------------------------------------------------------- #
def _uploaded(url: str) -> UploadResult:
    return UploadResult(source_url="source", onedrive_url=url)


def test_writeback_preserves_existing_links_and_adds_new_ones():
    notes = "Service notes\n\nOneDrive Backup:\n- https://drive/one"
    updated = _append_writeback(notes, [_uploaded("https://drive/two")])

    assert "Service notes" in updated
    assert "- https://drive/one" in updated
    assert "- https://drive/two" in updated


def test_writeback_deduplicates_links_and_preserves_following_text():
    notes = (
        "Before\n\nOneDrive Backup:\n- https://drive/one\n"
        "- https://drive/two\n\nWarranty information"
    )
    updated = _append_writeback(
        notes, [_uploaded("https://drive/two"), _uploaded("https://drive/three")]
    )

    assert updated.count("https://drive/two") == 1
    assert "https://drive/one" in updated
    assert "https://drive/three" in updated
    assert updated.endswith("Warranty information")


def test_asset_is_deferred_when_activity_history_is_unavailable():
    class UnavailableSnipe:
        def iter_asset_activity(self, asset_id):
            raise RuntimeError("temporary outage")

    result = process_asset(
        {"id": 7, "asset_tag": "AC-7", "notes": "https://example/image.jpg"},
        cfg=object(),
        snipe=UnavailableSnipe(),
        drive=object(),
        store=object(),
    )

    assert len(result) == 1
    assert result[0].skipped_reason == "activity_fetch_failed"


# --------------------------------------------------------------------- #
# Action labels — "checkin from" is a real Snipe-IT value, so exact
# comparison against "checkin" mislabelled every check-in as "Photos".
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "action,label",
    [
        ("checkout", "Check Out"),
        ("checkin", "Check In"),
        ("checkin from", "Check In"),
        ("update", "Update"),
        ("create", "Added"),
        ("", "Photos"),
    ],
)
def test_action_label(action, label):
    assert _action_label(action) == label


def test_event_subfolder_name_shape():
    assert _event_subfolder_name("checkout", "Alex Rivera", "2026-07-17 09:01:45") == (
        "Check Out - Alex Rivera - 2026-07-17 09-01"
    )


def test_event_subfolder_name_is_stable_across_runs():
    args = ("checkout", "Alex Rivera", "2026-07-17 09:01:45")
    assert _event_subfolder_name(*args) == _event_subfolder_name(*args)


def test_event_subfolder_name_drops_empty_parts():
    assert _event_subfolder_name("checkout", "", "") == "Check Out"


# --------------------------------------------------------------------- #
# Entry field extraction
#
# Preferring "formatted" over "datetime" gave parse_dt a lowercase am/pm
# string it couldn't read, so it returned now() and names churned every run.
# --------------------------------------------------------------------- #
def test_entry_date_prefers_machine_readable_datetime():
    entry = {"created_at": {"datetime": "2026-07-17 09:01:45",
                            "formatted": "July 17, 2026 9:01am"}}
    assert _extract_entry_date(entry) == "2026-07-17 09:01:45"


def test_entry_date_falls_back_to_formatted():
    assert _extract_entry_date({"created_at": {"formatted": "July 17, 2026 09:01 AM"}}) == (
        "July 17, 2026 09:01 AM"
    )


def test_entry_date_missing_returns_empty():
    assert _extract_entry_date({}) == ""


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"admin": {"name": "Alex Rivera"}}, "Alex Rivera"),
        ({"created_by": {"first_name": "Sam", "last_name": "Ortiz"}}, "Sam Ortiz"),
        ({"user": {"username": "jlee"}}, "jlee"),
        ({"admin": "Plain String"}, "Plain String"),
        ({}, ""),
    ],
)
def test_uploader_extraction(entry, expected):
    assert _extract_uploader_name(entry) == expected


# --------------------------------------------------------------------- #
# Matching a native upload to its activity entry
# --------------------------------------------------------------------- #
def _at(minutes: float) -> datetime:
    base = datetime(2026, 7, 17, 9, 1, 45, tzinfo=timezone.utc)
    return base + timedelta(minutes=minutes)


def test_upload_matches_the_entry_at_the_same_moment():
    entries = [activity(1)]
    assert _nearest_activity_entry(_at(0), entries)["id"] == 1


def test_upload_outside_the_window_matches_nothing():
    entries = [activity(1)]
    beyond = ACTIVITY_MATCH_WINDOW.total_seconds() / 60 + 1
    assert _nearest_activity_entry(_at(beyond), entries) is None


def test_upload_picks_the_nearest_of_several_entries():
    entries = [
        activity(1, when="2026-07-17 09:01:45"),
        activity(2, when="2026-07-17 09:02:30"),
    ]
    assert _nearest_activity_entry(_at(0), entries)["id"] == 1


def test_only_checkin_and_checkout_entries_are_considered():
    """An "update" at the same instant must not claim a checkout's photos."""
    assert _nearest_activity_entry(_at(0), [activity(1, action="update")]) is None
    assert _nearest_activity_entry(_at(0), [activity(2, action="checkin from")])["id"] == 2


def test_entries_without_dates_are_skipped():
    assert _nearest_activity_entry(_at(0), [{"action_type": "checkout"}]) is None


# --------------------------------------------------------------------- #
# Subfolder filenames
#
# Writing straight to "Model - {position}.jpg" overwrote whatever already
# held that name, because album order is not stable between runs.
# --------------------------------------------------------------------- #
class FakeDrive:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.checked = []

    def file_exists(self, folder, name):
        self.checked.append(name)
        return name in self.existing


class BrokenDrive:
    def file_exists(self, folder, name):
        raise RuntimeError("graph unavailable")


def _name(drive, position, replace=False, existing=()):
    return _subfolder_filename(
        drive=drive, folder="f", stem="Truck", ext="jpg",
        position=position, replace_existing=replace,
    )


def test_empty_folder_uses_the_position():
    assert _name(FakeDrive(), 1) == "Truck - 1.jpg"
    assert _name(FakeDrive(), 3) == "Truck - 3.jpg"


def test_new_photo_appends_rather_than_overwriting():
    drive = FakeDrive({"Truck - 1.jpg", "Truck - 2.jpg", "Truck - 3.jpg"})
    assert _name(drive, 2) == "Truck - 4.jpg"


def test_force_resync_overwrites_in_place():
    drive = FakeDrive({"Truck - 1.jpg", "Truck - 2.jpg"})
    assert _name(drive, 2, replace=True) == "Truck - 2.jpg"


def test_lookup_failure_falls_back_instead_of_spinning():
    assert _name(BrokenDrive(), 3) == "Truck - 3.jpg"


# --------------------------------------------------------------------- #
# The foldering rule itself
#
# Requiring more than one photo left single-photo checkouts loose in the
# model folder, mixed in with the event subfolders.
# --------------------------------------------------------------------- #
def _use_subfolder(source: str, batch_size: int) -> bool:
    return source == "activity" or batch_size > 1


@pytest.mark.parametrize(
    "source,size,expected",
    [
        ("activity", 1, True),
        ("activity", 5, True),
        ("asset_image", 1, False),
        ("top_notes", 1, False),
        ("top_notes", 3, True),
    ],
)
def test_every_event_gets_a_subfolder(source, size, expected):
    assert _use_subfolder(source, size) is expected


# --------------------------------------------------------------------- #
# Native upload destinations
#
# SnipeMobile 1.2.0 added a Files tab, so a photo can be attached without any
# check-in or check-out. Those have no event to name a folder after and used
# to land loose in the model folder alongside the event folders.
# --------------------------------------------------------------------- #
BASE = "AssetPhotos/Vehicle/Chevrolet Silverado 2025"


def test_upload_tied_to_a_checkout_goes_to_that_event_folder():
    assert _native_upload_folder(BASE, activity(1, action="checkout")) == (
        f"{BASE}/Check Out - Alex Rivera - 2026-07-17 09-01"
    )


def test_upload_tied_to_a_checkin_goes_to_that_event_folder():
    assert _native_upload_folder(BASE, activity(1, action="checkin from")) == (
        f"{BASE}/Check In - Alex Rivera - 2026-07-17 09-01"
    )


def test_upload_with_no_event_goes_to_the_file_upload_folder():
    assert _native_upload_folder(BASE, None) == f"{BASE}/{UNMATCHED_UPLOAD_FOLDER}"


def test_unmatched_upload_never_lands_loose_in_the_model_folder():
    """The regression: it used to return the model folder itself."""
    assert _native_upload_folder(BASE, None) != BASE


def test_file_upload_folder_is_shared_across_unmatched_uploads():
    assert _native_upload_folder(BASE, None) == _native_upload_folder(BASE, None)


# --------------------------------------------------------------------- #
# Skipping quiet album links
#
# Each album link was re-opened in a headless browser on every run. Quiet ones
# from old check-ins/outs are now checked daily. What must not change: a
# skipped album never counts as a failure (that would fail and alert every
# run), and anything recent, unchecked, forced, or outside a check-in/out
# note is still opened.
# --------------------------------------------------------------------- #
ALBUM = "https://photos.app.goo.gl/abc123"
OLD_DATE = "2026-01-05 09:00:00"


def _batch(source="activity", date=OLD_DATE):
    return _UrlBatch(urls=[ALBUM], uploader="Alex Rivera", date=date,
                     action_type="checkout", source=source)


def _cfg(force=False):
    return SimpleNamespace(force_resync=force, use_playwright=True)


class _Resolver:
    """Stands in for the headless-browser album resolver and counts calls."""

    def __init__(self, photos=()):
        self.photos = list(photos)
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        return self.photos


def _run(store, batch, cfg, resolver, monkeypatch):
    monkeypatch.setattr(sync, "resolve_google_photos_album", resolver)
    return _process_batch(
        batch, asset_id=1, asset_name="Truck", asset_tag="T-1", safe_tag="T-1",
        info={}, cfg=cfg, drive=object(), store=store, base_folder="AssetPhotos/Vehicle/Truck",
    )


@pytest.fixture
def uploaded_photo(tmp_path):
    """A store that already holds the one photo the album contains."""
    content = encode(make_photo(3))
    store = DedupeStore(tmp_path / "state.db")
    store.record_upload(
        asset_id=1, asset_tag="T-1", source_url="cdn-url", content_hash=hash_bytes(content),
        onedrive_file_id="f", onedrive_url="w", filename="Truck - 1.jpg",
    )
    photo = SimpleNamespace(content=content, mime_type="image/jpeg", extension="jpg",
                            final_url="cdn-url")
    return store, photo


def test_settled_link_is_counted_as_skipped_not_failed():
    assert "settled_link" in SKIPPED_REASONS


def test_quiet_old_album_is_opened_once_then_skipped(uploaded_photo, monkeypatch):
    store, photo = uploaded_photo
    resolver = _Resolver([photo])

    _run(store, _batch(), _cfg(), resolver, monkeypatch)
    results = _run(store, _batch(), _cfg(), resolver, monkeypatch)

    assert resolver.calls == 1
    assert [r.skipped_reason for r in results] == ["settled_link"]


def test_recent_event_album_is_opened_every_run(uploaded_photo, monkeypatch):
    store, photo = uploaded_photo
    resolver = _Resolver([photo])
    today = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    for _ in range(3):
        _run(store, _batch(date=today), _cfg(), resolver, monkeypatch)

    assert resolver.calls == 3


def test_only_check_in_out_links_are_ever_skipped(uploaded_photo, monkeypatch):
    store, photo = uploaded_photo
    resolver = _Resolver([photo])

    for _ in range(3):
        _run(store, _batch(source="top_notes"), _cfg(), resolver, monkeypatch)

    assert resolver.calls == 3


def test_force_resync_never_skips(uploaded_photo, monkeypatch):
    store, photo = uploaded_photo
    resolver = _Resolver([photo])
    _run(store, _batch(), _cfg(), resolver, monkeypatch)

    forced = _Resolver([])
    _run(store, _batch(), _cfg(force=True), forced, monkeypatch)

    assert forced.calls == 1


def test_a_failed_render_is_not_counted_as_a_check(tmp_path, monkeypatch):
    store = DedupeStore(tmp_path / "state.db")
    failing = _Resolver([])

    first = _run(store, _batch(), _cfg(), failing, monkeypatch)
    _run(store, _batch(), _cfg(), failing, monkeypatch)

    assert [r.skipped_reason for r in first] == ["unresolved"]
    assert failing.calls == 2
