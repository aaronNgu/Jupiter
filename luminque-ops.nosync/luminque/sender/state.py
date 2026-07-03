import json
import os
from pathlib import Path


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"last_sent_action_event_id": 0, "last_successful_send_utc": None, "server_session_id": None}
    return json.loads(state_path.read_text())


def save_state(state_path: Path, state: dict) -> None:
    tmp_path = state_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    os.replace(tmp_path, state_path)
