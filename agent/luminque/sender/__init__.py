"""
luminque.sender — reads the capture DB, (Phase 2: scrubs PII), and ships
screenshots to the Luminque ingestion service, one multipart request per
frame (POST /v1/screenshots), plus one POST /v1/heartbeat per cycle.

Runs as a short-lived process every 45 minutes via Task Scheduler.
Persistent cursor (sender_state.json) advances only on accepted frames —
at-least-once delivery; the server dedupes on (agent_id, captured_at).
Credentials are stored in Windows Credential Manager via keyring.
"""
import os
import sys


def _setup_logging():
    """Log to %APPDATA%\\Luminque\\logs\\sender.log, rotated nightly.

    The frozen exe runs --send through this run() (not sender/__main__.py), and
    the production build is windowed with no console — so without a file handler
    here a failed send cycle leaves no trace anywhere. That blind spot is what
    made field failures undebuggable. Mirrors capture's setup: a
    TimedRotatingFileHandler (backupCount bounds the directory) plus a
    StreamHandler only when a console is actually attached.
    """
    import logging
    from logging.handlers import TimedRotatingFileHandler
    from pathlib import Path

    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    log_dir = Path(appdata) / "Luminque" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers = [
        TimedRotatingFileHandler(
            str(log_dir / "sender.log"),
            when="midnight",
            backupCount=14,
            encoding="utf-8",
        )
    ]
    # In a windowed (console=False) PyInstaller build sys.stdout is None;
    # a StreamHandler on it would raise-and-swallow every record.
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
    )


def run() -> None:
    """Execute one send cycle."""
    _setup_logging()
    from luminque.sender.sender import run_sender
    sys.exit(run_sender())
