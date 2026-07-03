"""Contract test: a DB written by captureV2 is readable by the unmodified
sender stack (open_capture_db → query_batch → payload serialization →
cleanup → retention). This is the compatibility boundary that lets captureV2
replace the openadapt-capture wrapper without touching the wire format.
"""

import time

import pytest

from luminque.captureV2 import schema


@pytest.fixture
def capture_db(tmp_path):
    """A capture DB as captureV2 would produce it: screenshots + window
    events, plus a couple of hand-inserted action events (future writers /
    legacy leftovers) to prove the action path still works."""
    db_path = tmp_path / "recording.db"
    conn = schema.open_db(db_path)
    rec_ts = time.time() - 100
    rec_id = schema.insert_recording(
        conn, timestamp=rec_ts, monitor_width=1920, monitor_height=1080,
        task_description="luminque-background",
    )
    for i in range(3):
        schema.insert_screenshot(conn, rec_id, rec_ts, rec_ts + i, f"png{i}".encode())
    schema.insert_window_event(
        conn, rec_id, rec_ts, rec_ts, "Excel", 0, 0, 800, 600, "0x1",
    )
    # Action events written by a legacy DB / future event capture: one click,
    # one mouse move (the sender must filter the move).
    conn.execute(
        """INSERT INTO action_event
           (name, timestamp, recording_timestamp, recording_id, window_event_id)
           VALUES ('click', ?, ?, ?, 1)""",
        (rec_ts + 1, rec_ts, rec_id),
    )
    conn.execute(
        """INSERT INTO action_event
           (name, timestamp, recording_timestamp, recording_id)
           VALUES ('move', ?, ?, ?)""",
        (rec_ts + 2, rec_ts, rec_id),
    )
    conn.commit()
    conn.close()
    return db_path


def test_query_batch_reads_capturev2_db(capture_db):
    from luminque.sender.db import open_capture_db, query_batch

    session = open_capture_db(capture_db)
    actions, screenshots, window_events = query_batch(
        session, last_action_id=0, last_screenshot_id=0,
        action_limit=100, screenshot_limit=100,
    )
    assert len(screenshots) == 3
    assert all(s.png_data for s in screenshots)
    assert [a.name for a in actions] == ["click"]  # move filtered out
    assert len(window_events) == 1
    assert window_events[0].title == "Excel"


def test_screenshot_cursor_advances(capture_db):
    from luminque.sender.db import open_capture_db, query_batch

    session = open_capture_db(capture_db)
    _, screenshots, _ = query_batch(session, 0, 0, 100, 100)
    max_id = max(s.id for s in screenshots)
    _, remaining, _ = query_batch(session, 0, max_id, 100, 100)
    assert remaining == []


def test_payload_serializes_capturev2_rows(capture_db):
    from luminque.sender.db import open_capture_db, query_batch
    from luminque.sender.payload import build_events_request

    session = open_capture_db(capture_db)
    actions, screenshots, window_events = query_batch(session, 0, 0, 100, 100)
    body = build_events_request(actions, screenshots, window_events)

    by_type = {}
    for event in body["events"]:
        by_type.setdefault(event["type"], []).append(event)
    assert len(by_type["screenshot"]) == 3
    assert len(by_type["action_event"]) == 1
    assert len(by_type["window_event"]) == 1

    shot = by_type["screenshot"][0]["payload"]
    assert shot["filename"] == f"screenshot_{shot['id']}.png"
    # timestamps serialized to ISO 8601 strings
    assert isinstance(shot["timestamp"], str)
    assert "T" in shot["timestamp"]


def test_cleanup_nullifies_sent_screenshots(capture_db):
    from luminque.sender.db import (
        cleanup_sent_screenshots,
        open_capture_db,
        query_batch,
    )

    session = open_capture_db(capture_db)
    _, screenshots, _ = query_batch(session, 0, 0, 100, 100)
    cleanup_sent_screenshots(session, max(s.id for s in screenshots))
    _, remaining, _ = query_batch(session, 0, 0, 100, 100)
    assert remaining == []  # png_data is NULL → excluded from future batches


def test_retention_cap_nullifies_old_screenshots(capture_db):
    from luminque.sender.db import open_capture_db, query_batch
    from luminque.sender.retention import enforce_retention_cap

    session = open_capture_db(capture_db)
    purged = enforce_retention_cap(session)  # fixture rows are recent → kept
    assert purged == 0

    # Age the rows past the retention cap (8h) and re-run.
    from luminque.sender.models import Screenshot

    session.query(Screenshot).update(
        {"timestamp": time.time() - 25 * 3600}, synchronize_session=False
    )
    session.commit()
    # chunk_size=1 exercises the chunked-UPDATE path (bounded write locks)
    assert enforce_retention_cap(session, chunk_size=1) == 3
    _, remaining, _ = query_batch(session, 0, 0, 100, 100)
    assert remaining == []
