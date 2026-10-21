"""SQLite-backed deduplication store.

We track every (asset_id, source_url) pair we've ever uploaded, along with
the OneDrive file id, web url, content hash, and a timestamp.

The orchestrator consults this store before doing any network work for a
given URL, so a normal nightly run is mostly no-ops once the backlog is
processed.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

# Max differing bits between two 256-bit perceptual hashes for them to count as
# the same photo. Measured: 5 bits for a genuine re-fetch of one photo, 98+
# bits between different photos — so anything in the teens separates them with
# a wide margin on both sides.
PERCEPTUAL_MAX_DISTANCE = 20


SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id        INTEGER NOT NULL,
    asset_tag       TEXT,
    source_url      TEXT NOT NULL,
    content_hash    TEXT,
    onedrive_file_id TEXT,
    onedrive_url    TEXT,
    filename        TEXT,
    uploaded_at     INTEGER NOT NULL,
    UNIQUE (asset_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_uploads_hash ON uploads (content_hash);
CREATE INDEX IF NOT EXISTS idx_uploads_asset ON uploads (asset_id);

-- When each Google Photos album link was last opened, and when it last
-- yielded a photo not already uploaded. Lets a long-quiet album be checked
-- once a day instead of on every run. Times are Unix seconds.
CREATE TABLE IF NOT EXISTS link_checks (
    asset_id        INTEGER NOT NULL,
    source_url      TEXT NOT NULL,
    first_checked   INTEGER NOT NULL,
    last_checked    INTEGER NOT NULL,
    last_new_photo  INTEGER,
    PRIMARY KEY (asset_id, source_url)
);
"""


class DedupeStore:
    """Tiny wrapper around a SQLite db file."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the original schema shipped."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(uploads)")}
        if "perceptual_hash" not in existing:
            conn.execute("ALTER TABLE uploads ADD COLUMN perceptual_hash TEXT")
            log.info("Added perceptual_hash column to dedupe DB")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_uploads_phash "
            "ON uploads (asset_id, perceptual_hash)"
        )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def is_processed(self, asset_id: int, source_url: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM uploads WHERE asset_id = ? AND source_url = ?",
                (asset_id, source_url),
            ).fetchone()
        return row is not None

    def has_hash_for_asset(self, asset_id: int, content_hash: str) -> bool:
        """True if this asset already has a file with this exact hash uploaded.

        Useful when the same image appears under two different share URLs.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM uploads WHERE asset_id = ? AND content_hash = ?",
                (asset_id, content_hash),
            ).fetchone()
        return row is not None

    def has_perceptual_match_for_asset(
        self,
        asset_id: int,
        perceptual_hash: Optional[str],
        max_distance: int = PERCEPTUAL_MAX_DISTANCE,
    ) -> bool:
        """True if this asset already has a visually equivalent image uploaded.

        Compares by Hamming distance rather than equality. Sources re-serve
        the same photo with slightly different bytes, which shifts a few hash
        bits — measured at 5 bits out of 256 for a real re-fetch, against 98+
        bits between genuinely different photos. Anything within
        ``max_distance`` is the same picture.
        """
        if not perceptual_hash:
            return False
        try:
            target = int(perceptual_hash, 16)
        except ValueError:
            return False
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT perceptual_hash FROM uploads "
                "WHERE asset_id = ? AND perceptual_hash IS NOT NULL",
                (asset_id,),
            ).fetchall()
        for row in rows:
            try:
                candidate = int(row["perceptual_hash"], 16)
            except (ValueError, TypeError):
                continue
            if bin(target ^ candidate).count("1") <= max_distance:
                return True
        return False

    def backfill_perceptual_hash(
        self, asset_id: int, content_hash: str, perceptual_hash: Optional[str]
    ) -> None:
        """Attach a perceptual hash to an existing row that predates the column.

        Called when a photo is skipped by exact-hash match: without this the
        row keeps a NULL perceptual_hash and stays vulnerable to the source
        re-serving the same image with different bytes.
        """
        if not perceptual_hash:
            return
        with self._conn() as conn:
            conn.execute(
                "UPDATE uploads SET perceptual_hash = ? "
                "WHERE asset_id = ? AND content_hash = ? AND perceptual_hash IS NULL",
                (perceptual_hash, asset_id, content_hash),
            )

    def link_is_settled(
        self,
        asset_id: int,
        source_url: str,
        *,
        event_time: float,
        now: float,
        active_window: float,
        recheck_every: float,
    ) -> bool:
        """True if an album link can skip this run's check.

        A link stays active — checked every run — for ``active_window``
        seconds after its event and after it last yielded a new photo, since
        that is when a tech is still adding pictures. Once quiet past that, it
        is checked once per ``recheck_every`` seconds. A link never checked
        before is never settled.

        ``event_time`` is when the check-in/out happened. If it couldn't be
        parsed the caller passes "now", so the first check stands in for it
        rather than keeping the link active forever.
        """
        with self._conn() as conn:
            row = conn.execute(
                "SELECT first_checked, last_checked, last_new_photo FROM link_checks "
                "WHERE asset_id = ? AND source_url = ?",
                (asset_id, source_url),
            ).fetchone()
        if row is None:
            return False
        active_since = max(
            min(event_time, row["first_checked"]), row["last_new_photo"] or 0
        )
        if now - active_since < active_window:
            return False
        return now - row["last_checked"] < recheck_every

    def record_link_check(
        self,
        asset_id: int,
        source_url: str,
        *,
        found_new: bool,
        now: Optional[float] = None,
    ) -> None:
        """Note that an album link was opened, and whether it had new photos.

        Call only after the album actually resolved; a failed render must not
        count as a check, or the link would wait a day for its retry.
        """
        ts = int(now if now is not None else time.time())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO link_checks
                  (asset_id, source_url, first_checked, last_checked, last_new_photo)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (asset_id, source_url) DO UPDATE SET
                  last_checked = excluded.last_checked,
                  last_new_photo = COALESCE(excluded.last_new_photo, link_checks.last_new_photo)
                """,
                (asset_id, source_url, ts, ts, ts if found_new else None),
            )


    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def record_upload(
        self,
        *,
        asset_id: int,
        asset_tag: str,
        source_url: str,
        content_hash: str,
        onedrive_file_id: Optional[str],
        onedrive_url: Optional[str],
        filename: str,
        perceptual_hash: Optional[str] = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO uploads
                  (asset_id, asset_tag, source_url, content_hash,
                   onedrive_file_id, onedrive_url, filename, uploaded_at,
                   perceptual_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset_id,
                    asset_tag,
                    source_url,
                    content_hash,
                    onedrive_file_id,
                    onedrive_url,
                    filename,
                    int(time.time()),
                    perceptual_hash,
                ),
            )
        log.debug(
            "Recorded upload asset_id=%s url=%s file=%s",
            asset_id,
            source_url,
            filename,
        )
