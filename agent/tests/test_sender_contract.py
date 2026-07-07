"""Contract test: a DB written by captureV2 is readable by the sender stack
(open_capture_db → query_unsent_screenshots → window_for_screenshot →
cleanup → retention). This is the compatibility boundary that lets captureV2
and the sender evolve independently of each other's process.
"""

import time

import pytest

from luminque.captureV2 import schema

REC_TS = time.time() - 100


@pytest.fixture
def capture_db(tmp_path):
    """A capture DB as captureV2 would produce it: screenshots plus window
    events stamped on foreground change (not per frame), plus a couple of
    hand-inserted action events (legacy leftovers) to prove the sender
    ignores them entirely."""
    db_path = tmp_path / "recording.db"
    conn = schema.open_db(db_path)
    rec_id = schema.insert_recording(
        conn, timestamp=REC_TS, monitor_width=1920, monitor_height=1080,
        task_description="luminque-background",
    )
    # Window changes at frame 0 (Excel) and frame 2 (Chrome); frame 1 keeps
    # the Excel window — captureV2 dedupes unchanged windows.
    schema.insert_window_event(
        conn, rec_id, REC_TS, REC_TS, "Excel", 0, 0, 800, 600, "0x1",
    )
    schema.insert_window_event(
        conn, rec_id, REC_TS, REC_TS + 2, "Chrome", 0, 0, 800, 600, "0x2",
    )
    for i in range(3):
        schema.insert_screenshot(conn, rec_id, REC_TS, REC_TS + i, f"png{i}".encode())
    # Action events from a legacy DB: the sender must not touch them.
    conn.execute(
        """INSERT INTO action_event
           (name, timestamp, recording_timestamp, recording_id, window_event_id)
           VALUES ('click', ?, ?, ?, 1)""",
        (REC_TS + 1, REC_TS, rec_id),
    )
    conn.commit()
    conn.close()
    return db_path


def test_query_unsent_screenshots_reads_capturev2_db(capture_db):
    from luminque.sender.db import open_capture_db, query_unsent_screenshots

    session = open_capture_db(capture_db)
    screenshots = query_unsent_screenshots(session, last_screenshot_id=0, limit=100)
    assert len(screenshots) == 3
    assert all(s.png_data for s in screenshots)
    assert [bytes(s.png_data) for s in screenshots] == [b"png0", b"png1", b"png2"]


def test_screenshot_cursor_advances(capture_db):
    from luminque.sender.db import open_capture_db, query_unsent_screenshots

    session = open_capture_db(capture_db)
    screenshots = query_unsent_screenshots(session, 0, 100)
    max_id = max(s.id for s in screenshots)
    assert query_unsent_screenshots(session, max_id, 100) == []


def test_window_for_screenshot_joins_latest_stamp(capture_db):
    """Per-frame window title = latest window_event at or before the frame.
    captureV2 stamps window events on change only, so the middle frame must
    inherit the earlier stamp, and the change frame picks up the new one."""
    from luminque.sender.db import (
        open_capture_db,
        query_unsent_screenshots,
        window_for_screenshot,
    )

    session = open_capture_db(capture_db)
    frames = query_unsent_screenshots(session, 0, 100)
    titles = [window_for_screenshot(session, s).title for s in frames]
    assert titles == ["Excel", "Excel", "Chrome"]


def test_window_for_screenshot_stays_within_recording(capture_db):
    """A frame from a later recording must not inherit a stale window stamp
    from an earlier one — no stamp in its own recording means no title."""
    from luminque.sender.db import (
        open_capture_db,
        query_unsent_screenshots,
        window_for_screenshot,
    )

    conn = schema.open_db(capture_db)
    rec2 = schema.insert_recording(conn, timestamp=REC_TS + 50)
    schema.insert_screenshot(conn, rec2, REC_TS + 50, REC_TS + 51, b"png-rec2")
    conn.close()

    session = open_capture_db(capture_db)
    frames = query_unsent_screenshots(session, 0, 100)
    orphan = [s for s in frames if bytes(s.png_data) == b"png-rec2"][0]
    assert window_for_screenshot(session, orphan) is None


def test_cleanup_nullifies_sent_screenshots(capture_db):
    from luminque.sender.db import (
        cleanup_sent_screenshots,
        open_capture_db,
        query_unsent_screenshots,
    )

    session = open_capture_db(capture_db)
    screenshots = query_unsent_screenshots(session, 0, 100)
    cleanup_sent_screenshots(session, max(s.id for s in screenshots))
    # png_data is NULL → excluded from future batches
    assert query_unsent_screenshots(session, 0, 100) == []


def test_retention_cap_nullifies_old_screenshots(capture_db):
    from luminque.sender.db import open_capture_db, query_unsent_screenshots
    from luminque.sender.retention import enforce_retention_cap

    session = open_capture_db(capture_db)
    purged = enforce_retention_cap(session)  # fixture rows are recent → kept
    assert purged == 0

    # Age the rows past the retention cap and re-run.
    from luminque.sender.models import Screenshot

    session.query(Screenshot).update(
        {"timestamp": time.time() - 25 * 3600}, synchronize_session=False
    )
    session.commit()
    # chunk_size=1 exercises the chunked-UPDATE path (bounded write locks)
    assert enforce_retention_cap(session, chunk_size=1) == 3
    assert query_unsent_screenshots(session, 0, 100) == []
