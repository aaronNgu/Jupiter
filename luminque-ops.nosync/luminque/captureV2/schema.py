"""SQLite data layer for captureV2 — stdlib sqlite3, no SQLAlchemy.

The DDL matches the table/column layout of openadapt-capture's models
(openadapt_capture/db/models.py) exactly, so the sender's queries, retention
cap, and post-upload cleanup work unchanged against a DB produced by either
capturer. `CREATE TABLE IF NOT EXISTS` lets captureV2 reopen and append to a
DB created by the legacy capturer with no migration; unsent rows keep
draining through the sender's existing cursors.

`action_event` is created even though captureV2 never writes to it: the
sender's query_batch() queries the table unconditionally.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS recording (
        id INTEGER PRIMARY KEY,
        timestamp NUMERIC(10, 2),
        monitor_width INTEGER,
        monitor_height INTEGER,
        double_click_interval_seconds NUMERIC,
        double_click_distance_pixels NUMERIC,
        platform VARCHAR,
        task_description VARCHAR,
        video_start_time NUMERIC(10, 2),
        config JSON,
        original_recording_id INTEGER REFERENCES recording (id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS screenshot (
        id INTEGER PRIMARY KEY,
        recording_timestamp NUMERIC(10, 2),
        recording_id INTEGER REFERENCES recording (id),
        timestamp NUMERIC(10, 2),
        png_data BLOB,
        png_diff_data BLOB,
        png_diff_mask_data BLOB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS window_event (
        id INTEGER PRIMARY KEY,
        recording_timestamp NUMERIC(10, 2),
        recording_id INTEGER REFERENCES recording (id),
        timestamp NUMERIC(10, 2),
        state JSON,
        title VARCHAR,
        "left" INTEGER,
        "top" INTEGER,
        width INTEGER,
        height INTEGER,
        window_id VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS action_event (
        id INTEGER PRIMARY KEY,
        name VARCHAR,
        timestamp NUMERIC(10, 2),
        recording_timestamp NUMERIC(10, 2),
        recording_id INTEGER REFERENCES recording (id),
        screenshot_timestamp NUMERIC(10, 2),
        screenshot_id INTEGER REFERENCES screenshot (id),
        window_event_timestamp NUMERIC(10, 2),
        window_event_id INTEGER REFERENCES window_event (id),
        browser_event_timestamp NUMERIC(10, 2),
        browser_event_id INTEGER,
        mouse_x NUMERIC,
        mouse_y NUMERIC,
        mouse_dx NUMERIC,
        mouse_dy NUMERIC,
        active_segment_description VARCHAR,
        available_segment_descriptions VARCHAR,
        mouse_button_name VARCHAR,
        mouse_pressed BOOLEAN,
        key_name VARCHAR,
        key_char VARCHAR,
        key_vk VARCHAR,
        canonical_key_name VARCHAR,
        canonical_key_char VARCHAR,
        canonical_key_vk VARCHAR,
        parent_id INTEGER REFERENCES action_event (id),
        element_state JSON,
        disabled BOOLEAN
    )
    """,
]


def open_db(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the capture DB with sender-compatible PRAGMAs.

    WAL + busy_timeout mirror what the sender sets on its side (sender/db.py):
    capture writes and sender reads must not block each other.
    """
    conn = sqlite3.connect(str(db_path))
    # auto_vacuum only takes effect on a *new* DB (before any table exists);
    # on an existing file it is a silent no-op until a full VACUUM. So this
    # enables file-shrink for fresh installs; existing DBs still bound their
    # size via page reuse (nulled pages are reused by new inserts → the file
    # plateaus rather than growing). Eviction is the hard bound; vacuum is the
    # best-effort space-return. Must precede DDL.
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    for ddl in _DDL:
        conn.execute(ddl)
    conn.commit()
    return conn


def total_blob_bytes(conn: sqlite3.Connection) -> int:
    """Sum of stored screenshot blob bytes. This — not the file size — is the
    quantity the disk guard bounds; SQLite does not shrink the file on null
    without VACUUM, so file size lags actual blob usage."""
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(png_data)), 0) FROM screenshot "
        "WHERE png_data IS NOT NULL"
    ).fetchone()
    return int(row[0])


def _null_ids(conn: sqlite3.Connection, ids: list[int]) -> None:
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE screenshot SET png_data=NULL, png_diff_data=NULL, "
        f"png_diff_mask_data=NULL WHERE id IN ({placeholders})",
        ids,
    )
    conn.commit()


def null_blobs_older_than(
    conn: sqlite3.Connection, cutoff_timestamp: float, chunk_size: int = 500
) -> int:
    """Null png_data for screenshots older than cutoff. Chunked + committed per
    batch so the write lock is never held past capture's busy_timeout (same
    reasoning as the sender's retention cap)."""
    total = 0
    while True:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM screenshot WHERE png_data IS NOT NULL "
                "AND timestamp < ? ORDER BY id ASC LIMIT ?",
                (cutoff_timestamp, chunk_size),
            ).fetchall()
        ]
        if not ids:
            break
        _null_ids(conn, ids)
        total += len(ids)
    return total


def null_oldest_blobs_until_under(
    conn: sqlite3.Connection, max_bytes: int, chunk_size: int = 500
) -> int:
    """Evict oldest-first (ascending id) until total blob bytes <= max_bytes.
    Chunked: may overshoot by up to one chunk's worth, which is fine for a
    disk bound and keeps the lock short."""
    total = 0
    while total_blob_bytes(conn) > max_bytes:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM screenshot WHERE png_data IS NOT NULL "
                "ORDER BY id ASC LIMIT ?",
                (chunk_size,),
            ).fetchall()
        ]
        if not ids:
            break
        _null_ids(conn, ids)
        total += len(ids)
    return total


def incremental_vacuum(conn: sqlite3.Connection) -> None:
    """Return freed pages to the OS. No-op (silently) when auto_vacuum is not
    INCREMENTAL — e.g. existing DBs created before this was enabled."""
    try:
        conn.execute("PRAGMA incremental_vacuum")
        conn.commit()
    except Exception:
        pass


def insert_recording(
    conn: sqlite3.Connection,
    timestamp: float | None = None,
    monitor_width: int | None = None,
    monitor_height: int | None = None,
    task_description: str = "",
    config: dict | None = None,
) -> int:
    if timestamp is None:
        timestamp = time.time()
    cur = conn.execute(
        """
        INSERT INTO recording
            (timestamp, monitor_width, monitor_height, platform,
             task_description, config)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            monitor_width,
            monitor_height,
            sys.platform,
            task_description,
            json.dumps(config or {}),
        ),
    )
    conn.commit()
    return cur.lastrowid


def insert_screenshot(
    conn: sqlite3.Connection,
    recording_id: int,
    recording_timestamp: float,
    timestamp: float,
    png_data: bytes,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO screenshot (recording_timestamp, recording_id, timestamp, png_data)
        VALUES (?, ?, ?, ?)
        """,
        (recording_timestamp, recording_id, timestamp, png_data),
    )
    conn.commit()
    return cur.lastrowid


def insert_window_event(
    conn: sqlite3.Connection,
    recording_id: int,
    recording_timestamp: float,
    timestamp: float,
    title: str,
    left: int | None,
    top: int | None,
    width: int | None,
    height: int | None,
    window_id: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO window_event
            (recording_timestamp, recording_id, timestamp, title,
             "left", "top", width, height, window_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            recording_timestamp,
            recording_id,
            timestamp,
            title,
            left,
            top,
            width,
            height,
            window_id,
        ),
    )
    conn.commit()
    return cur.lastrowid
