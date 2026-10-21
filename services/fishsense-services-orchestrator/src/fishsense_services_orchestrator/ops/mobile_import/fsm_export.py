"""Reading a phone's own database, as FishSense Mobile exported it.

New in v2 (2026-10-21). Before sync, the app kept every capture in one SQLite
file per phone (a ``photos`` table: the photo's name, its UTC time, and the
LiDAR depth and confidence as blobs). The import copies that file off the NAS
into a temp dir and reads it here, read-only.

This is the one module outside the API package with SQL in it, and
tests/test_database_ownership.py names it as such: the SQL is for the phone's
file, never for our database, which only the API package's catalogs touch.
"""

from __future__ import annotations

import sqlite3

from fishsense_services_orchestrator.ops.mobile_import.plan import FolderRow

__all__ = ["read_blobs", "read_rows"]


def _open(path: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def read_rows(path: str) -> list[FolderRow]:
    """Every row, with its blobs' sizes but not the blobs."""
    con = _open(path)
    try:
        return [FolderRow(*row) for row in con.execute("""
                SELECT id, utc_unix_timestamp, rgb_path, depth_width, depth_height,
                       length(depth_bytes), confidence_width, confidence_height,
                       length(confidence_bytes)
                FROM photos ORDER BY id
                """)]
    finally:
        con.close()


def read_blobs(path: str, row_id: int) -> tuple[bytes, bytes]:
    """A row's depth (float32) and confidence (uint8), as stored."""
    con = _open(path)
    try:
        return con.execute(
            "SELECT depth_bytes, confidence_bytes FROM photos WHERE id = ?", (row_id,)
        ).fetchone()
    finally:
        con.close()
