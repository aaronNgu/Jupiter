"""Integration: run_sender end to end against a local HTTP server speaking
the v1 ingestion contract, from a captureV2-produced fixture capture DB.

Pins the sender-side invariants of design-docs/luminque-ingestion-p1.md:
one multipart request per frame, cursor advance on 200 and 201 (never on
5xx), heartbeat once per cycle even with nothing to upload, X-Device-Token
as the only identity.
"""

import json
import threading
import time
from datetime import datetime, timezone
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from luminque.captureV2 import schema
from luminque.sender.sender import run_sender
from luminque.sender.state import load_state

# Recent (must stay inside the 6h retention cap — run_sender enforces it at
# the end of every cycle, including failed ones) but fixed per test run, so
# expected captured_at values are computable.
REC_TS = round(time.time()) - 60.0
TOKEN = "test-device-token"


def _parse_multipart(content_type: str, body: bytes) -> dict:
    """Parse a multipart/form-data body into {field_name: bytes}."""
    msg = BytesParser().parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    )
    assert msg.is_multipart(), "screenshot upload must be multipart/form-data"
    return {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in msg.get_payload()
    }


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        srv = self.server
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        record = {"path": self.path, "headers": dict(self.headers)}
        srv.requests.append(record)

        if self.path == "/v1/heartbeat":
            self.send_response(204)
            self.end_headers()
            return

        if self.path == "/v1/screenshots":
            fields = _parse_multipart(self.headers["Content-Type"], body)
            record["fields"] = fields
            if srv.scripted_statuses:
                status = srv.scripted_statuses.pop(0)
            else:
                # Real-server behavior: dedupe on captured_at → 201 new, 200 dup
                captured_at = fields["captured_at"]
                status = 200 if captured_at in srv.stored else 201
                srv.stored.add(captured_at)
            payload = json.dumps({"id": "srv-id"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []
    srv.stored = set()
    srv.scripted_statuses = []  # tests push statuses to force failures/dups
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    thread.join()


@pytest.fixture
def sender_env(tmp_path, monkeypatch, server):
    """APPDATA in a tmp dir, credentials answered without keyring, no proxy
    interference for the loopback server."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    creds = {"auth_token": TOKEN, "endpoint_url": server.url}
    monkeypatch.setattr(
        "luminque.sender.credentials.get_credential", lambda key: creds[key]
    )
    return tmp_path


def _build_capture_db(appdata: Path, frames: int = 3) -> None:
    rec_dir = appdata / "Luminque" / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    conn = schema.open_db(rec_dir / "recording.db")
    rec_id = schema.insert_recording(conn, timestamp=REC_TS)
    schema.insert_window_event(
        conn, rec_id, REC_TS, REC_TS, "Invoice.xlsx - Excel", 0, 0, 800, 600, "0x1"
    )
    for i in range(frames):
        schema.insert_screenshot(conn, rec_id, REC_TS, REC_TS + i, f"png-{i}".encode())
    conn.close()


def _screenshot_requests(server):
    return [r for r in server.requests if r["path"] == "/v1/screenshots"]


def _heartbeat_requests(server):
    return [r for r in server.requests if r["path"] == "/v1/heartbeat"]


def _expected_captured_at(i: int) -> str:
    return datetime.fromtimestamp(REC_TS + i, tz=timezone.utc).isoformat()


def test_full_cycle_per_frame_multipart(server, sender_env):
    _build_capture_db(sender_env)

    assert run_sender() == 0

    shots = _screenshot_requests(server)
    assert len(shots) == 3  # one request per frame, no envelope
    for i, req in enumerate(shots):
        assert req["headers"]["X-Device-Token"] == TOKEN
        assert "Authorization" not in req["headers"]
        assert req["fields"]["file"] == f"png-{i}".encode()
        assert req["fields"]["captured_at"].decode() == _expected_captured_at(i)
        assert req["fields"]["window_title"].decode() == "Invoice.xlsx - Excel"
    assert len(_heartbeat_requests(server)) == 1

    state = load_state(sender_env / "Luminque" / "sender_state.json")
    assert state["last_sent_screenshot_id"] == 3
    assert state["last_successful_send_utc"] is not None

    # Second cycle: everything already sent (and blobs nulled) → heartbeat only
    assert run_sender() == 0
    assert len(_screenshot_requests(server)) == 3
    assert len(_heartbeat_requests(server)) == 2


def test_cursor_advances_on_200_duplicates(server, sender_env):
    """200 = server-side duplicate; the cursor must advance exactly as on 201."""
    _build_capture_db(sender_env)
    server.scripted_statuses = [201, 200, 200]

    assert run_sender() == 0

    state = load_state(sender_env / "Luminque" / "sender_state.json")
    assert state["last_sent_screenshot_id"] == 3


def test_no_cursor_advance_on_5xx_and_partial_resume(server, sender_env):
    """A 5xx stops the cycle without advancing past the accepted frames, so
    the next cycle resends only from the failed frame — never the whole batch."""
    _build_capture_db(sender_env)
    server.scripted_statuses = [201, 500]

    assert run_sender() == 1

    shots = _screenshot_requests(server)
    assert len(shots) == 2  # frame 3 never attempted after the failure
    state_path = sender_env / "Luminque" / "sender_state.json"
    state = load_state(state_path)
    assert state["last_sent_screenshot_id"] == 1  # frame 1 accepted, frame 2 not
    assert state["last_successful_send_utc"] is None
    assert len(_heartbeat_requests(server)) == 1  # heartbeat still went out

    # Recovery cycle: only frames 2 and 3 go out
    assert run_sender() == 0
    resent = _screenshot_requests(server)[2:]
    assert [r["fields"]["captured_at"].decode() for r in resent] == [
        _expected_captured_at(1),
        _expected_captured_at(2),
    ]
    assert load_state(state_path)["last_sent_screenshot_id"] == 3


def test_heartbeat_sent_when_nothing_to_upload(server, sender_env):
    _build_capture_db(sender_env, frames=0)

    assert run_sender() == 0

    assert _screenshot_requests(server) == []
    assert len(_heartbeat_requests(server)) == 1
    assert _heartbeat_requests(server)[0]["headers"]["X-Device-Token"] == TOKEN


def test_heartbeat_sent_when_no_capture_db(server, sender_env):
    assert run_sender() == 0
    assert [r["path"] for r in server.requests] == ["/v1/heartbeat"]
