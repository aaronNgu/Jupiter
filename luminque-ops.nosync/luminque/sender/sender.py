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


def run_sender() -> int:
    """Main entry point. Returns 0 on success, 1 on failure."""
    from luminque.sender.constants import DB_FILENAME, MAX_BATCH_EVENTS, MAX_BATCH_SCREENSHOTS, STATE_FILENAME
    from luminque.sender.credentials import get_credential
    from luminque.sender.db import cleanup_sent_screenshots, open_capture_db, query_batch
    from luminque.sender.heartbeat import build_health_report
    from luminque.sender.payload import build_events_request, build_session_request
    from luminque.sender.retention import enforce_retention_cap
    from luminque.sender.state import load_state, save_state
    from luminque.sender.payload import screenshot_filename
    from luminque.sender.transport import create_session, post_events, post_health, post_media

    appdata_dir = _get_appdata_dir()
    appdata_dir.mkdir(parents=True, exist_ok=True)
    state_path = appdata_dir / STATE_FILENAME

    try:
        state = load_state(state_path)
    except Exception as e:
        logger.error(f"Failed to load state: {e}")
        return 1

    last_sent_id = state.get("last_sent_action_event_id", 0)
    last_sent_screenshot_id = state.get("last_sent_screenshot_id", 0)
    server_session_id = state.get("server_session_id")

    try:
        api_key = get_credential("api_key")
        base_url = get_credential("endpoint_url")
        tenant_id = get_credential("tenant_id")
        device_id = get_credential("device_id")
    except RuntimeError as e:
        logger.error(str(e))
        return 1

    db_path = _find_db(appdata_dir, DB_FILENAME)
    session = None
    action_events, screenshots, window_events = [], [], []

    if db_path is None:
        logger.warning("No capture DB found — sending health report only")
    else:
        try:
            session = open_capture_db(db_path)
        except Exception as e:
            logger.error(f"Failed to open capture DB: {e}")
            return 1
        try:
            action_events, screenshots, window_events = query_batch(
                session,
                last_action_id=last_sent_id,
                last_screenshot_id=last_sent_screenshot_id,
                action_limit=MAX_BATCH_EVENTS,
                screenshot_limit=MAX_BATCH_SCREENSHOTS,
            )
        except Exception as e:
            logger.error(f"Failed to query batch: {e}")
            return 1

    health_body = build_health_report(
        machine_id=device_id,
        tenant_id=tenant_id,
        appdata_dir=appdata_dir,
        queue_depth=len(action_events),
        last_successful_send_utc=state.get("last_successful_send_utc"),
    )
    try:
        post_health(health_body, base_url, api_key)
    except Exception as e:
        logger.warning(f"Health report failed (non-fatal): {e}")

    if not action_events and not screenshots:
        state["last_successful_send_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            save_state(state_path, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return 1
        if session:
            _run_retention(session)
        return 0

    if not server_session_id:
        # Derive session start from first action event, or first screenshot timestamp
        if action_events:
            rec_ts = action_events[0].recording_timestamp
        else:
            rec_ts = screenshots[0].recording_timestamp if hasattr(screenshots[0], "recording_timestamp") else screenshots[0].timestamp
        started_at = datetime.fromtimestamp(rec_ts, tz=timezone.utc).isoformat()
        session_body = build_session_request(device_id, tenant_id, started_at)
        try:
            resp = create_session(session_body, base_url, api_key)
        except Exception as e:
            logger.error(f"Failed to create server session: {e}")
            if session:
                _run_retention(session)
            return 1
        if resp.status_code != 201:
            logger.error(f"Session creation failed {resp.status_code}: {resp.text[:500]}")
            if session:
                _run_retention(session)
            return 1
        server_session_id = resp.json()["id"]
        state["server_session_id"] = server_session_id
        logger.info(f"Created server session {server_session_id}")

    for s in screenshots:
        if s.png_data:
            try:
                resp = post_media(server_session_id, screenshot_filename(s.id), s.png_data, base_url, api_key)
                if resp.status_code != 201:
                    logger.error(f"Media upload failed for screenshot {s.id}: {resp.status_code} {resp.text[:200]}")
                    if session:
                        _run_retention(session)
                    return 1
            except Exception as e:
                logger.error(f"Media upload failed for screenshot {s.id}: {e}")
                if session:
                    _run_retention(session)
                return 1

    events_body = build_events_request(action_events, screenshots, window_events)
    try:
        response = post_events(server_session_id, events_body, base_url, api_key)
    except Exception as e:
        logger.error(f"POST events failed: {e}")
        if session:
            _run_retention(session)
        return 1

    if response.status_code == 201:
        if action_events:
            state["last_sent_action_event_id"] = max(e.id for e in action_events)
        if screenshots:
            max_screenshot_id = max(s.id for s in screenshots)
            state["last_sent_screenshot_id"] = max_screenshot_id
            if session:
                cleanup_sent_screenshots(session, max_screenshot_id)
        state["last_successful_send_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            save_state(state_path, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return 1
        logger.info(
            f"Sent {len(action_events)} events, "
            f"{len(screenshots)} screenshots, "
            f"{len(window_events)} window events"
        )
    elif response.status_code == 404:
        logger.warning(f"Server session {server_session_id} not found — will recreate next cycle")
        state["server_session_id"] = None
        try:
            save_state(state_path, state)
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
        if session:
            _run_retention(session)
        return 1
    elif 400 <= response.status_code < 500:
        logger.error(f"Non-retryable error {response.status_code}: {response.text[:500]}")
        if session:
            _run_retention(session)
        return 1
    else:
        logger.error(f"Server error {response.status_code}: {response.text[:500]}")
        if session:
            _run_retention(session)
        return 1

    if session:
        _run_retention(session)
    return 0


def _run_retention(session) -> None:
    try:
        from luminque.sender.retention import enforce_retention_cap
        purged = enforce_retention_cap(session)
        if purged:
            logger.info(f"Retention cap: nullified {purged} screenshot blobs")
    except Exception as e:
        logger.warning(f"Retention cap failed: {e}")
