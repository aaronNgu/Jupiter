"""
luminque.captureV2 — native screenshot capture (openadapt-capture replacement).

Runs as a long-lived process (ONLOGON via Task Scheduler). One process, one
capture thread plus storage-free pynput listeners. Writes screenshots and
window events to the same SQLite database the legacy capturer used:
%APPDATA%\\Luminque\\recordings\\recording.db (new Recording row per start,
IDs keep incrementing). See design-docs/luminque-capture-p3.md.
"""

import os
import sys

# Held for the process lifetime; releasing the handle would release the mutex.
_mutex_handle = None


def _setup_logging():
    """Log to %APPDATA%\\Luminque\\logs\\capture.log, rotated nightly.

    Capture runs detached (no console attached), so an uncaught exception
    otherwise leaves no trace. A file handler is the only way to see why
    it dies.

    TimedRotatingFileHandler instead of a date-stamped filename: the process
    runs for days, and the legacy date-in-name scheme only rolled over because
    the watchdog restarted capture at midnight — a machine asleep during the
    restart window kept appending to a stale-dated file forever, and nothing
    ever pruned the directory. backupCount bounds disk use.
    """
    import logging
    from logging.handlers import TimedRotatingFileHandler
    from pathlib import Path

    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    log_dir = Path(appdata) / "Luminque" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers = [
        TimedRotatingFileHandler(
            str(log_dir / "capture.log"),
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
    return logging.getLogger("luminque.captureV2")


def _acquire_single_instance_lock() -> bool:
    """Named-mutex guard: the watchdog can fire while capture is running.

    Session-local namespace ("Local\\"): non-elevated processes cannot create
    "Global\\" objects (requires SeCreateGlobalPrivilege), and capture is
    per-user-session by design (Task Scheduler ONLOGON). Known limitation: on
    multi-session hosts (RDS, fast user switching) each session runs its own
    capture against the same per-user DB — WAL keeps that safe, just
    redundant. Returns False if another instance holds the mutex.
    Non-Windows: always True (dev only; production is Windows).
    """
    global _mutex_handle
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    ERROR_ALREADY_EXISTS = 183
    # use_last_error captures the error code at FFI-call time;
    # windll.kernel32.GetLastError() called afterwards can be clobbered by
    # ctypes' own intervening Win32 calls.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    _mutex_handle = kernel32.CreateMutexW(None, False, "Local\\LuminqueCapture")
    if not _mutex_handle:
        return True  # mutex unavailable — do not block capture on the guard
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def run() -> None:
    """Start the capture process."""
    from pathlib import Path

    log = _setup_logging()
    # Keep the exact "Capture starting" phrasing — ops runbooks grep for it.
    log.info("Capture starting (pid=%s, capturer=captureV2)", os.getpid())

    if not _acquire_single_instance_lock():
        log.info("Another capture instance is already running — exiting")
        return

    from luminque.captureV2 import constants, schema
    from luminque.captureV2.activity import ActivityMonitor
    from luminque.captureV2.grabber import Grabber
    from luminque.captureV2.loop import CaptureLoop

    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    capture_dir = Path(appdata) / "Luminque" / "recordings"
    capture_dir.mkdir(parents=True, exist_ok=True)
    db_path = capture_dir / constants.DB_FILENAME
    log.info("Recording to %s", db_path)

    conn = schema.open_db(db_path)
    activity = ActivityMonitor()
    activity.start()
    loop = CaptureLoop(conn=conn, grabber=Grabber(), activity=activity)

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        log.info("Interrupted — shutting down")
    except Exception:
        log.exception("Capture crashed")
        raise
    finally:
        activity.stop()
        try:
            conn.close()
        except Exception:
            pass
        log.info("Capture process exiting (pid=%s)", os.getpid())
