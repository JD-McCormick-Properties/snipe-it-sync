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
"""


class DedupeStore:
    """Tiny wrapper around a SQLite db file."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

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

    def count_for_asset(self, asset_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM uploads WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

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
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO uploads
                  (asset_id, asset_tag, source_url, content_hash,
                   onedrive_file_id, onedrive_url, filename, uploaded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
        log.debug(
            "Recorded upload asset_id=%s url=%s file=%s",
            asset_id,
            source_url,
            filename,
        )
