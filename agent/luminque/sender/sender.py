import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_appdata_dir() -> Path:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return Path(appdata) / "Luminque"


def _get_recordings_dir(appdata_dir: Path) -> Path:
    return appdata_dir / "recordings"


def _find_db(appdata_dir: Path, db_filename: str) -> Path | None:
    recordings_dir = _get_recordings_dir(appdata_dir)
    flat = recordings_dir / db_filename
    if flat.exists():
        return flat
    if recordings_dir.exists():
        for session_dir in sorted(
            (d for d in recordings_dir.iterdir() if d.is_dir()),
            key=lambda d: d.name,
            reverse=True,
        ):
            candidate = session_dir / db_filename
            if candidate.exists():
                return candidate
    return None


def _captured_at_iso(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


def run_sender() -> int:
    """Main entry point. Returns 0 on success, 1 on failure."""
    from luminque.sender.constants import DB_FILENAME, MAX_BATCH_SCREENSHOTS, STATE_FILENAME
    from luminque.sender.credentials import get_credential
    from luminque.sender.db import (
        cleanup_sent_screenshots,
        open_capture_db,
        query_unsent_screenshots,
        window_for_screenshot,
    )
    from luminque.sender.state import load_state, save_state
    from luminque.sender.transport import post_heartbeat, post_screenshot

    appdata_dir = _get_appdata_dir()
    appdata_dir.mkdir(parents=True, exist_ok=True)
    state_path = appdata_dir / STATE_FILENAME

    try:
        state = load_state(state_path)
    except Exception as e:
        logger.error(f"Failed to load state: {e}")
        return 1

    try:
        auth_token = get_credential("auth_token")
        base_url = get_credential("endpoint_url")
    except RuntimeError as e:
        logger.error(str(e))
        return 1

    # Heartbeat first, once per cycle, even with nothing to upload — it
    # signals "sender ran and reached the server". Never fatal.
    try:
        resp = post_heartbeat(base_url, auth_token)
        if resp.status_code >= 400:
            logger.warning(f"Heartbeat failed (non-fatal): HTTP {resp.status_code}")
    except Exception as e:
        logger.warning(f"Heartbeat failed (non-fatal): {e}")

    db_path = _find_db(appdata_dir, DB_FILENAME)
    if db_path is None:
        logger.warning("No capture DB found — heartbeat only")
        return 0

    try:
        session = open_capture_db(db_path)
    except Exception as e:
        logger.error(f"Failed to open capture DB: {e}")
        return 1

    try:
        screenshots = query_unsent_screenshots(
            session,
            last_screenshot_id=state.get("last_sent_screenshot_id", 0),
            limit=MAX_BATCH_SCREENSHOTS,
        )
    except Exception as e:
        logger.error(f"Failed to query screenshots: {e}")
        return 1

    sent = 0
    failed = False
    for s in screenshots:
        window = window_for_screenshot(session, s)
        try:
            resp = post_screenshot(
                png_bytes=s.png_data,
                captured_at=_captured_at_iso(s.timestamp),
                window_title=window.title if window else None,
                app_name=None,  # not recorded by captureV2 (foreground.py has no process info)
                base_url=base_url,
                auth_token=auth_token,
            )
        except Exception as e:
            logger.error(f"Screenshot {s.id} upload failed: {e}")
            failed = True
            break
        # 201 = stored, 200 = server-side duplicate — both advance the cursor,
        # so a retried batch never resends frames the server already has.
        if resp.status_code not in (200, 201):
            logger.error(
                f"Screenshot {s.id} upload failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
            failed = True
            break
        state["last_sent_screenshot_id"] = s.id
        try:
            save_state(state_path, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            failed = True
            break
        sent += 1

    if sent:
        try:
            cleanup_sent_screenshots(session, state["last_sent_screenshot_id"])
        except Exception as e:
            logger.warning(f"Post-upload cleanup failed: {e}")

    if not failed:
        state["last_successful_send_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            save_state(state_path, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            failed = True
        if sent:
            logger.info(f"Sent {sent} of {len(screenshots)} screenshots")

    _run_retention(session)
    return 1 if failed else 0


def _run_retention(session) -> None:
    try:
        from luminque.sender.retention import enforce_retention_cap
        purged = enforce_retention_cap(session)
        if purged:
            logger.info(f"Retention cap: nullified {purged} screenshot blobs")
    except Exception as e:
        logger.warning(f"Retention cap failed: {e}")
