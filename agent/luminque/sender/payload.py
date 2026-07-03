import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _ts(unix: float | None) -> str | None:
    if unix is None:
        return None
    return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat()

_ACTION_EVENT_FIELDS = [
    "id", "name", "timestamp", "recording_timestamp", "recording_id",
    "screenshot_id", "window_event_id",
    "mouse_x", "mouse_y", "mouse_dx", "mouse_dy",
    "mouse_button_name", "mouse_pressed",
    "key_name", "key_char", "key_vk",
    "canonical_key_name", "canonical_key_char", "canonical_key_vk",
    "active_segment_description",
    "parent_id", "element_state", "disabled",
]

_WINDOW_EVENT_FIELDS = [
    "id", "recording_id", "recording_timestamp", "timestamp",
    "title", "left", "top", "width", "height", "window_id", "state",
]


_TIMESTAMP_FIELDS = {"timestamp", "recording_timestamp"}


def screenshot_filename(screenshot_id: int) -> str:
    return f"screenshot_{screenshot_id}.png"


def serialize_action_event(e) -> dict:
    result = {
        f: (_ts(getattr(e, f, None)) if f in _TIMESTAMP_FIELDS else getattr(e, f, None))
        for f in _ACTION_EVENT_FIELDS
    }
    result["available_segment_descriptions"] = getattr(
        e, "_available_segment_descriptions", None
    )
    sid = result.get("screenshot_id")
    result["screenshot_filename"] = screenshot_filename(sid) if sid is not None else None
    return result


def serialize_screenshot(s) -> dict:
    return {
        "id": s.id,
        "recording_id": s.recording_id,
        "recording_timestamp": _ts(s.recording_timestamp),
        "timestamp": _ts(s.timestamp),
        "filename": screenshot_filename(s.id),
    }


def serialize_window_event(w) -> dict:
    result = {
        f: (_ts(getattr(w, f, None)) if f in _TIMESTAMP_FIELDS else getattr(w, f, None))
        for f in _WINDOW_EVENT_FIELDS
    }
    return result


def _wrap(event_type: str, data: dict) -> dict:
    return {
        "type": event_type,
        "timestamp": data.get("timestamp"),
        "payload": data,
    }


def build_events_request(action_events, screenshots, window_events) -> dict:
    events = (
        [_wrap("action_event", serialize_action_event(e)) for e in action_events]
        + [_wrap("screenshot", serialize_screenshot(s)) for s in screenshots]
        + [_wrap("window_event", serialize_window_event(w)) for w in window_events]
    )
    return {"events": events}


def build_session_request(machine_id: str, tenant_id: str, started_at: str) -> dict:
    return {
        "device_id": machine_id,
        "tenant_id": tenant_id,
        "started_at": started_at,
        "metadata": {},
    }
