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
