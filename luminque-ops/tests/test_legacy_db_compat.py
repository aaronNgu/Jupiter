"""Legacy-DB compatibility: captureV2 must append to a recording.db created
by the openadapt-capture wrapper (SQLAlchemy create_all), and the sender must
read mixed legacy + captureV2 rows. This pins the upgrade path for machines
in the field — design doc luminque-capture-p3.md §4 / §8.

tests/fixtures/legacy_recording.sql was dumped from a DB built by the
openadapt-capture fork's Base.metadata.create_all() plus one unsent
recording/screenshot/window_event/action_event row. To regenerate: in the
openadapt-capture repo, create_all into a temp sqlite file, insert the rows
(see fixture comments), and write conn.iterdump() over the fixture file.
"""

import sqlite3
from pathlib import Path

import pytest

from luminque.captureV2 import schema

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_recording.sql"


@pytest.fixture
def legacy_db(tmp_path):
    db_path = tmp_path / "recording.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(FIXTURE.read_text())
    conn.close()
    return db_path


def test_capturev2_appends_to_legacy_db(legacy_db):
    conn = schema.open_db(legacy_db)  # IF NOT EXISTS — must not error or alter
    rec_id = schema.insert_recording(conn, task_description="luminque-background")
    shot_id = schema.insert_screenshot(conn, rec_id, 2.0, 2.0, b"v2-png")
    win_id = schema.insert_window_event(
        conn, rec_id, 2.0, 2.0, "V2 Window", 0, 0, 800, 600, "7"
    )
    # Legacy fixture rows have id=1 in each table; IDs must continue, not reset.
    assert rec_id == 2
    assert shot_id == 2
    assert win_id == 2
    conn.close()


def test_sender_reads_mixed_legacy_and_v2_rows(legacy_db):
    from luminque.sender.db import open_capture_db, query_batch
    from luminque.sender.payload import build_events_request

    conn = schema.open_db(legacy_db)
    rec_id = schema.insert_recording(conn, task_description="luminque-background")
    schema.insert_screenshot(conn, rec_id, 2.0, 2.0, b"v2-png")
    conn.close()

    session = open_capture_db(legacy_db)
    actions, screenshots, window_events = query_batch(session, 0, 0, 100, 100)
    assert [a.name for a in actions] == ["click"]  # legacy unsent action
    assert len(screenshots) == 2  # one legacy + one captureV2
    assert {bytes(s.png_data) for s in screenshots} == {
        b"legacy-png-bytes",
        b"v2-png",
    }
    assert [w.title for w in window_events] == ["Legacy Window"]

    body = build_events_request(actions, screenshots, window_events)
    assert len(body["events"]) == 4


def test_schema_matches_legacy_columns_exactly(legacy_db, tmp_path):
    """captureV2 DDL and the legacy SQLAlchemy DDL must agree on column
    names per table — this is what makes the two interchangeable."""
    v2_conn = schema.open_db(tmp_path / "v2.db")
    legacy_conn = sqlite3.connect(legacy_db)
    for table in ("recording", "screenshot", "window_event", "action_event"):
        v2_cols = [r[1] for r in v2_conn.execute(f"PRAGMA table_info({table})")]
        legacy_cols = [r[1] for r in legacy_conn.execute(f"PRAGMA table_info({table})")]
        assert v2_cols == legacy_cols, f"column drift in {table}"
    v2_conn.close()
    legacy_conn.close()
