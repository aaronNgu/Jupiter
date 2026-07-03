import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def open_capture_db(db_path: Path):
    """Open capture DB session. Returns SQLAlchemy Session.

    Two PRAGMAs are set immediately after connecting:
    - journal_mode=WAL  — allows the sender (reader) and capture (writer) to
      run concurrently without blocking each other. The default DELETE journal
      mode gives capture an exclusive lock that blocks all sender reads.
    - busy_timeout=5000 — if a lock conflict does occur, SQLite waits up to
      5 s before raising "database is locked" instead of failing immediately.
    """
    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect", insert=True)
    def _set_sqlite_pragmas(dbapi_conn, _conn_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    session = sessionmaker(bind=engine)()

    # Apply to the already-open connection in this session.
    session.execute(text("PRAGMA journal_mode=WAL"))
    session.execute(text("PRAGMA busy_timeout=5000"))

    return session


def query_batch(
    session,
    last_action_id: int,
    last_screenshot_id: int,
    action_limit: int,
    screenshot_limit: int,
) -> tuple:
    """Returns (action_events, screenshots, window_events) for the batch.

    Mouse-move events are excluded — they are pure cursor-position noise and
    carry no meaningful information for SOP discovery.  Skipping them here also
    keeps batches dense with signal (clicks, scrolls, keypresses) and prevents
    the 5 000-move-per-session problem seen in early recordings.

    Screenshots are queried independently using their own ID cursor.
    OpenAdapt does not reliably populate ``action_event.screenshot_id`` (the
    FK is NULL on all events in practice), so the old approach of collecting
    screenshot IDs from action events silently sent zero screenshots.
    """
    from luminque.sender.models import ActionEvent, Screenshot, WindowEvent

    action_events = (
        session.query(ActionEvent)
        .filter(ActionEvent.id > last_action_id)
        .filter(ActionEvent.name != "move")
        .order_by(ActionEvent.id.asc())
        .limit(action_limit)
        .all()
    )

    # Fetch screenshots with their own cursor — independent of action_event FKs.
    # Only send screenshots that have actual pixel data.
    screenshots = (
        session.query(Screenshot)
        .filter(Screenshot.id > last_screenshot_id)
        .filter(Screenshot.png_data != None)  # noqa: E711
        .order_by(Screenshot.id.asc())
        .limit(screenshot_limit)
        .all()
    )

    window_event_ids = {
        e.window_event_id for e in action_events if e.window_event_id is not None
    }
    window_events = (
        session.query(WindowEvent)
        .filter(WindowEvent.id.in_(window_event_ids))
        .all()
        if window_event_ids
        else []
    )

    return action_events, screenshots, window_events


def cleanup_sent_screenshots(session, max_screenshot_id: int) -> None:
    """Null out png_data for screenshots already uploaded to free local disk."""
    from luminque.sender.models import Screenshot

    session.query(Screenshot).filter(
        Screenshot.id <= max_screenshot_id,
        Screenshot.png_data != None,  # noqa: E711
    ).update(
        {
            "png_data": None,
            "png_diff_data": None,
            "png_diff_mask_data": None,
        },
        synchronize_session=False,
    )
    session.commit()
