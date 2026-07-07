import json
from unittest.mock import MagicMock, patch

import pytest


class TestStateLoadSave:
    def test_load_missing_returns_defaults(self, tmp_path):
        from luminque.sender.state import load_state
        state = load_state(tmp_path / "nonexistent.json")
        assert state["last_sent_screenshot_id"] == 0
        assert state["last_successful_send_utc"] is None

    def test_save_then_load_roundtrip(self, tmp_path):
        from luminque.sender.state import load_state, save_state
        p = tmp_path / "state.json"
        data = {"last_sent_screenshot_id": 42, "last_successful_send_utc": "2026-01-01T00:00:00Z"}
        save_state(p, data)
        result = load_state(p)
        assert result["last_sent_screenshot_id"] == 42

    def test_save_is_atomic(self, tmp_path):
        from luminque.sender.state import save_state
        p = tmp_path / "state.json"
        save_state(p, {"last_sent_screenshot_id": 1, "last_successful_send_utc": None})
        assert p.exists()
        assert not (tmp_path / "state.tmp").exists()

    def test_old_contract_keys_ignored_and_dropped(self, tmp_path):
        """State files written under the pre-v1 contract carry cursors that no
        longer exist (action-event cursor, server session). Loading must not
        fail on them, must keep the screenshot cursor, and must not carry the
        dead keys forward into the next save."""
        from luminque.sender.state import load_state, save_state
        p = tmp_path / "state.json"
        p.write_text(json.dumps({
            "last_sent_action_event_id": 900,
            "last_sent_screenshot_id": 7,
            "server_session_id": "sess-1",
            "last_successful_send_utc": "2026-01-01T00:00:00Z",
        }))
        state = load_state(p)
        assert state["last_sent_screenshot_id"] == 7
        assert "last_sent_action_event_id" not in state
        assert "server_session_id" not in state

        save_state(p, state)
        on_disk = json.loads(p.read_text())
        assert "last_sent_action_event_id" not in on_disk
        assert "server_session_id" not in on_disk


class TestTransport:
    def test_post_heartbeat_url_and_empty_body(self):
        from luminque.sender.transport import post_heartbeat
        with patch("requests.post", return_value=MagicMock(status_code=204)) as mock_post:
            post_heartbeat("http://localhost:8000", "tok")
            assert mock_post.call_args[0][0] == "http://localhost:8000/v1/heartbeat"
            assert mock_post.call_args[1]["verify"] is True
            assert "json" not in mock_post.call_args[1]
            assert "data" not in mock_post.call_args[1]

    def test_post_screenshot_url_and_multipart_fields(self):
        from luminque.sender.transport import post_screenshot
        with patch("requests.post", return_value=MagicMock(status_code=201)) as mock_post:
            post_screenshot(
                png_bytes=b"png-bytes",
                captured_at="2026-07-04T09:15:00+00:00",
                window_title="Invoice.xlsx - Excel",
                app_name="EXCEL.EXE",
                base_url="http://localhost:8000",
                auth_token="tok",
            )
            assert mock_post.call_args[0][0] == "http://localhost:8000/v1/screenshots"
            assert mock_post.call_args[1]["verify"] is True
            files = mock_post.call_args[1]["files"]
            filename, content, content_type = files["file"]
            assert content == b"png-bytes"
            assert content_type == "image/png"
            data = mock_post.call_args[1]["data"]
            assert data["captured_at"] == "2026-07-04T09:15:00+00:00"
            assert data["window_title"] == "Invoice.xlsx - Excel"
            assert data["app_name"] == "EXCEL.EXE"

    def test_post_screenshot_omits_absent_window_fields(self):
        from luminque.sender.transport import post_screenshot
        with patch("requests.post", return_value=MagicMock(status_code=201)) as mock_post:
            post_screenshot(
                png_bytes=b"x",
                captured_at="2026-07-04T09:15:00+00:00",
                window_title=None,
                app_name=None,
                base_url="http://localhost:8000",
                auth_token="tok",
            )
            data = mock_post.call_args[1]["data"]
            assert "window_title" not in data
            assert "app_name" not in data

    def test_device_token_header_only_no_bearer(self):
        """Identity comes solely from X-Device-Token — the old Authorization
        bearer duplicate must be gone."""
        from luminque.sender.transport import post_heartbeat, post_screenshot
        with patch("requests.post", return_value=MagicMock(status_code=201)) as mock_post:
            for call in (
                lambda: post_heartbeat("http://localhost:8000", "tok"),
                lambda: post_screenshot(
                    b"x", "2026-07-04T09:15:00+00:00", None, None,
                    "http://localhost:8000", "tok",
                ),
            ):
                call()
                headers = mock_post.call_args[1]["headers"]
                assert headers["X-Device-Token"] == "tok"
                assert "Authorization" not in headers


class TestSenderCursorBehavior:
    def test_cursor_not_advanced_on_server_error(self, tmp_path):
        from luminque.sender.state import load_state, save_state
        initial_state = {"last_sent_screenshot_id": 100, "last_successful_send_utc": None}
        state_path = tmp_path / "sender_state.json"
        save_state(state_path, initial_state)
        state = load_state(state_path)
        assert state["last_sent_screenshot_id"] == 100
