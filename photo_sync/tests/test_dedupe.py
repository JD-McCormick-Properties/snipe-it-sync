"""Dedupe store: schema migration and the matching rules that gate uploads."""

from __future__ import annotations

import sqlite3

import pytest

from helpers.dedupe import PERCEPTUAL_MAX_DISTANCE, DedupeStore


def _hash(bits: str) -> str:
    """A 256-bit hash from a bit pattern, zero-padded."""
    return f"{int(bits, 2):064x}"


ZERO = _hash("0")


def flip(h: str, n: int) -> str:
    """Same hash with n low bits inverted."""
    return f"{int(h, 16) ^ int('1' * n, 2):064x}" if n else h


@pytest.fixture
def store(tmp_path) -> DedupeStore:
    return DedupeStore(tmp_path / "state.db")


def _record(store: DedupeStore, **kw):
    args = dict(
        asset_id=1, asset_tag="AT-1", source_url="u1", content_hash="c1",
        onedrive_file_id="f1", onedrive_url="w1", filename="a.jpg",
    )
    args.update(kw)
    store.record_upload(**args)


# --------------------------------------------------------------------- #
# Migration — the column was added after the DB shipped
# --------------------------------------------------------------------- #
def test_migration_adds_column_and_preserves_rows(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER NOT NULL, asset_tag TEXT, source_url TEXT NOT NULL,
            content_hash TEXT, onedrive_file_id TEXT, onedrive_url TEXT,
            filename TEXT, uploaded_at INTEGER NOT NULL,
            UNIQUE (asset_id, source_url)
        );
        """
    )
    conn.execute(
        "INSERT INTO uploads (asset_id, source_url, content_hash, filename, uploaded_at)"
        " VALUES (1,'u1','c1','a.jpg',0)"
    )
    conn.commit()
    conn.close()

    DedupeStore(path)

    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(uploads)")}
    assert "perceptual_hash" in cols
    assert conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "state.db"
    DedupeStore(path)
    _record(DedupeStore(path))
    DedupeStore(path)  # opening again must not wipe or error
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1


# --------------------------------------------------------------------- #
# Exact matching
# --------------------------------------------------------------------- #
def test_is_processed_is_scoped_to_the_asset(store):
    _record(store, asset_id=1, source_url="u1")
    assert store.is_processed(1, "u1")
    assert not store.is_processed(2, "u1")


def test_has_hash_for_asset_is_scoped_to_the_asset(store):
    _record(store, asset_id=1, content_hash="abc")
    assert store.has_hash_for_asset(1, "abc")
    assert not store.has_hash_for_asset(2, "abc")


# --------------------------------------------------------------------- #
# Perceptual matching
#
# Comparing these with == meant a re-fetch that shifted 5 bits counted as a
# new photo, so albums kept re-uploading even after perceptual hashing landed.
# --------------------------------------------------------------------- #
def test_identical_hash_matches(store):
    _record(store, perceptual_hash=ZERO)
    assert store.has_perceptual_match_for_asset(1, ZERO)


def test_small_drift_matches(store):
    """5 bits is the drift measured against Google's CDN in production."""
    _record(store, perceptual_hash=ZERO)
    assert store.has_perceptual_match_for_asset(1, flip(ZERO, 5))


def test_drift_at_the_threshold_matches(store):
    _record(store, perceptual_hash=ZERO)
    assert store.has_perceptual_match_for_asset(1, flip(ZERO, PERCEPTUAL_MAX_DISTANCE))


def test_drift_beyond_the_threshold_does_not_match(store):
    """A genuinely different photo must still be uploaded."""
    _record(store, perceptual_hash=ZERO)
    assert not store.has_perceptual_match_for_asset(1, flip(ZERO, PERCEPTUAL_MAX_DISTANCE + 1))


def test_perceptual_match_is_scoped_to_the_asset(store):
    _record(store, asset_id=1, perceptual_hash=ZERO)
    assert not store.has_perceptual_match_for_asset(2, ZERO)


def test_none_hash_never_matches(store):
    """Undecodable images fall back to exact hashing rather than matching all."""
    _record(store, perceptual_hash=ZERO)
    assert not store.has_perceptual_match_for_asset(1, None)


def test_malformed_stored_hash_is_skipped_not_fatal(store):
    _record(store, source_url="bad", perceptual_hash="not-hex")
    _record(store, source_url="good", perceptual_hash=ZERO)
    assert store.has_perceptual_match_for_asset(1, ZERO)


# --------------------------------------------------------------------- #
# Backfill — without it, pre-existing rows stayed NULL and kept re-uploading
# --------------------------------------------------------------------- #
def test_backfill_populates_a_null_row(store):
    _record(store, content_hash="c1", perceptual_hash=None)
    store.backfill_perceptual_hash(1, "c1", ZERO)
    assert store.has_perceptual_match_for_asset(1, ZERO)


def test_backfill_does_not_overwrite_an_existing_hash(store):
    _record(store, content_hash="c1", perceptual_hash=ZERO)
    store.backfill_perceptual_hash(1, "c1", flip(ZERO, 200))
    assert store.has_perceptual_match_for_asset(1, ZERO)


def test_backfill_with_none_is_a_no_op(store):
    _record(store, content_hash="c1", perceptual_hash=ZERO)
    store.backfill_perceptual_hash(1, "c1", None)
    assert store.has_perceptual_match_for_asset(1, ZERO)


# --------------------------------------------------------------------- #
# Album re-check schedule
#
# Every album link was re-opened in a headless browser on every run, which
# was nearly all of each run's time. A quiet album is now checked daily —
# but a recent or still-growing one must never be skipped.
# --------------------------------------------------------------------- #
DAY = 86_400
WINDOW = 3 * DAY
RECHECK = DAY
NOW = 1_800_000_000.0
OLD_EVENT = NOW - 30 * DAY


def _settled(store, *, event_time=OLD_EVENT, now=NOW, url="album"):
    return store.link_is_settled(
        1, url, event_time=event_time, now=now,
        active_window=WINDOW, recheck_every=RECHECK,
    )


def test_a_never_checked_link_is_not_settled(store):
    assert not _settled(store)


def test_quiet_old_album_checked_recently_is_settled(store):
    store.record_link_check(1, "album", found_new=False, now=NOW - 2 * 3600)
    assert _settled(store)


def test_quiet_old_album_is_rechecked_once_a_day(store):
    store.record_link_check(1, "album", found_new=False, now=NOW - RECHECK - 60)
    assert not _settled(store)


def test_recent_event_is_checked_every_run(store):
    store.record_link_check(1, "album", found_new=False, now=NOW - 60)
    assert not _settled(store, event_time=NOW - 2 * DAY)


def test_album_that_just_gained_photos_stays_active(store):
    store.record_link_check(1, "album", found_new=True, now=NOW - 60)
    assert not _settled(store)


def test_a_quiet_check_does_not_erase_a_recent_new_photo(store):
    store.record_link_check(1, "album", found_new=True, now=NOW - DAY)
    store.record_link_check(1, "album", found_new=False, now=NOW - 60)
    assert not _settled(store)


def test_activity_ends_after_the_window(store):
    store.record_link_check(1, "album", found_new=True, now=NOW - WINDOW - 3600)
    store.record_link_check(1, "album", found_new=False, now=NOW - 60)
    assert _settled(store)


def test_unreadable_event_date_falls_back_to_the_first_check(store):
    """The caller passes "now" when a date won't parse; the link must still
    settle eventually instead of staying active forever."""
    store.record_link_check(1, "album", found_new=False, now=NOW - 10 * DAY)
    store.record_link_check(1, "album", found_new=False, now=NOW - 60)
    assert _settled(store, event_time=NOW)


def test_link_checks_are_scoped_to_the_asset(store):
    store.record_link_check(2, "album", found_new=False, now=NOW - 60)
    assert not _settled(store)


def test_existing_db_gains_the_link_checks_table(tmp_path):
    """The cached production DB predates the table; opening it must add it
    without touching the upload records."""
    path = tmp_path / "state.db"
    _record(DedupeStore(path))
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE link_checks")
    conn.commit()
    conn.close()

    store = DedupeStore(path)
    store.record_link_check(1, "album", found_new=False, now=NOW - 60)
    assert _settled(store)
    assert store.is_processed(1, "u1")
