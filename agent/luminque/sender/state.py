import json
import os
from pathlib import Path

# Known keys only. Anything else in an old state file (pre-v1-contract keys
# like last_sent_action_event_id or server_session_id) is ignored on load and
# disappears on the next save.
_DEFAULTS = {
    "last_sent_screenshot_id": 0,
    "last_successful_send_utc": None,
}


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return dict(_DEFAULTS)
    raw = json.loads(state_path.read_text())
    return {key: raw.get(key, default) for key, default in _DEFAULTS.items()}


def save_state(state_path: Path, state: dict) -> None:
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, state_path)
