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


def query_unsent_screenshots(session, last_screenshot_id: int, limit: int) -> list:
    """Screenshots past the cursor that still hold pixel data, oldest first.

    Rows whose png_data was nulled (retention cap / capture-side disk guard)
    are skipped permanently — there is nothing left to upload for them.
    """
    from luminque.sender.models import Screenshot

    return (
        session.query(Screenshot)
        .filter(Screenshot.id > last_screenshot_id)
        .filter(Screenshot.png_data != None)  # noqa: E711
        .order_by(Screenshot.id.asc())
        .limit(limit)
        .all()
    )


def window_for_screenshot(session, screenshot):
    """The window_event governing a frame, or None.

    captureV2 inserts a window_event only when the foreground window changes,
    stamped with the same wall-clock timestamp as the screenshot that
    triggered it — so the row governing a frame is the latest one at or
    before the frame's timestamp. Same recording only: after a capture
    restart the first frames may legitimately precede any window stamp
    (UIPI blocks foreground reads in elevated contexts), and a previous
    recording's window is stale information, not a fallback.
    """
    from luminque.sender.models import WindowEvent

    return (
        session.query(WindowEvent)
        .filter(WindowEvent.recording_id == screenshot.recording_id)
        .filter(WindowEvent.timestamp <= screenshot.timestamp)
        .order_by(WindowEvent.timestamp.desc(), WindowEvent.id.desc())
        .first()
    )


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
