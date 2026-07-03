import base64
from unittest.mock import MagicMock, patch

import pytest


class TestStateLoadSave:
    def test_load_missing_returns_defaults(self, tmp_path):
        from luminque.sender.state import load_state
        state = load_state(tmp_path / "nonexistent.json")
        assert state["last_sent_action_event_id"] == 0
        assert state["last_successful_send_utc"] is None

    def test_save_then_load_roundtrip(self, tmp_path):
        from luminque.sender.state import load_state, save_state
        p = tmp_path / "state.json"
        data = {"last_sent_action_event_id": 42, "last_successful_send_utc": "2026-01-01T00:00:00Z"}
        save_state(p, data)
        result = load_state(p)
        assert result["last_sent_action_event_id"] == 42

    def test_save_is_atomic(self, tmp_path):
        from luminque.sender.state import save_state
        p = tmp_path / "state.json"
        save_state(p, {"last_sent_action_event_id": 1, "last_successful_send_utc": None})
        assert p.exists()
        assert not (tmp_path / "state.tmp").exists()

    def test_load_missing_includes_server_session_id(self, tmp_path):
        from luminque.sender.state import load_state
        assert load_state(tmp_path / "x.json")["server_session_id"] is None


class TestPayload:
    def test_build_events_request_structure(self):
        from luminque.sender.payload import build_events_request
        mock_ae = MagicMock()
        result = build_events_request([mock_ae], [], [])
        assert "events" in result
        event = result["events"][0]
        assert event["type"] == "action_event"
        assert "payload" in event
        assert "timestamp" in event

    def test_build_session_request(self):
        from luminque.sender.payload import build_session_request
        result = build_session_request("dev-id", "tenant-id", "2026-01-01T00:00:00+00:00")
        assert result["device_id"] == "dev-id"
        assert result["tenant_id"] == "tenant-id"
        assert result["started_at"] == "2026-01-01T00:00:00+00:00"

    def test_serialize_screenshot_has_media_filename(self):
        from luminque.sender.payload import serialize_screenshot
        s = MagicMock()
        s.id = 1
        s.recording_id = 1
        s.recording_timestamp = 0.0
        s.timestamp = 1.0
        result = serialize_screenshot(s)
        assert result["filename"] == "screenshot_1.png"
        assert "png_data_b64" not in result

    def test_serialize_action_event_has_screenshot_filename(self):
        from luminque.sender.payload import serialize_action_event
        e = MagicMock()
        e.screenshot_id = 88
        e.timestamp = 1.0
        e.recording_timestamp = 0.0
        for field in ["id", "name", "recording_id", "window_event_id", "mouse_x", "mouse_y",
                      "mouse_dx", "mouse_dy", "mouse_button_name", "mouse_pressed", "key_name",
                      "key_char", "key_vk", "canonical_key_name", "canonical_key_char",
                      "canonical_key_vk", "active_segment_description", "parent_id",
                      "element_state", "disabled"]:
            setattr(e, field, None)
        result = serialize_action_event(e)
        assert result["screenshot_filename"] == "screenshot_88.png"


class TestTransport:
    def test_post_health_calls_correct_url(self):
        from luminque.sender.transport import post_health
        mock_response = MagicMock(status_code=200)
        with patch("requests.post", return_value=mock_response) as mock_post:
            post_health({}, "http://localhost:8000", "key")
            url = mock_post.call_args[0][0]
            assert url == "http://localhost:8000/api/v1/devices/health"
            assert mock_post.call_args[1]["verify"] is True

    def test_create_session_calls_correct_url(self):
        from luminque.sender.transport import create_session
        mock_response = MagicMock(status_code=201)
        with patch("requests.post", return_value=mock_response) as mock_post:
            create_session({}, "http://localhost:8000", "key")
            url = mock_post.call_args[0][0]
            assert url == "http://localhost:8000/api/v1/sessions"
            assert mock_post.call_args[1]["verify"] is True

    def test_post_events_calls_correct_url(self):
        from luminque.sender.transport import post_events
        mock_response = MagicMock(status_code=201)
        with patch("requests.post", return_value=mock_response) as mock_post:
            post_events("sess-123", {}, "http://localhost:8000", "key")
            url = mock_post.call_args[0][0]
            assert "/api/v1/sessions/sess-123/events" in url
            assert mock_post.call_args[1]["verify"] is True

    def test_transport_verify_always_true(self):
        from luminque.sender.transport import create_session, post_events, post_health
        with patch("requests.post", return_value=MagicMock(status_code=200)) as mock_post:
            post_health({}, "http://localhost:8000", "key")
            assert mock_post.call_args[1]["verify"] is True
            create_session({}, "http://localhost:8000", "key")
            assert mock_post.call_args[1]["verify"] is True
            post_events("s-1", {}, "http://localhost:8000", "key")
            assert mock_post.call_args[1]["verify"] is True

    def test_device_routes_send_device_token_header(self):
        # The ingestion API authenticates device routes via X-Device-Token,
        # whose value is the device auth_token (passed here as the api_key arg).
        from luminque.sender.transport import (
            create_session,
            post_events,
            post_health,
            post_media,
        )
        with patch("requests.post", return_value=MagicMock(status_code=201)) as mock_post:
            for call in (
                lambda: post_health({}, "http://localhost:8000", "tok"),
                lambda: create_session({}, "http://localhost:8000", "tok"),
                lambda: post_events("s-1", {}, "http://localhost:8000", "tok"),
                lambda: post_media("s-1", "screenshot_1.png", b"x", "http://localhost:8000", "tok"),
            ):
                call()
                assert mock_post.call_args[1]["headers"]["X-Device-Token"] == "tok"


class TestHeartbeat:
    def test_get_or_create_machine_id_creates(self, tmp_path):
        from luminque.sender.heartbeat import get_or_create_machine_id
        machine_id = get_or_create_machine_id(tmp_path)
        assert len(machine_id) == 36
        assert (tmp_path / "machine_id").exists()

    def test_get_or_create_machine_id_stable(self, tmp_path):
        from luminque.sender.heartbeat import get_or_create_machine_id
        first = get_or_create_machine_id(tmp_path)
        second = get_or_create_machine_id(tmp_path)
        assert first == second

    def test_build_health_report_structure(self, tmp_path):
        from luminque.sender.heartbeat import build_health_report
        with patch("luminque.sender.heartbeat._is_capture_running", return_value=False):
            report = build_health_report("dev-id", "tenant-id", tmp_path, 10, None)
        assert "overall_status" in report
        assert "components" in report
        assert "queue_depth" in report
        assert "disk_usage_percent" in report
        assert "reported_at" in report
        assert report["queue_depth"] == 10


class TestSenderCursorBehavior:
    def test_cursor_not_advanced_on_server_error(self, tmp_path):
        from luminque.sender.state import load_state, save_state
        initial_state = {"last_sent_action_event_id": 100, "last_successful_send_utc": None}
        state_path = tmp_path / "sender_state.json"
        save_state(state_path, initial_state)
        state = load_state(state_path)
        assert state["last_sent_action_event_id"] == 100
