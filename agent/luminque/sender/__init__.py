"""
luminque.sender — reads the capture DB, (Phase 2: scrubs PII), and ships
screenshots to the Luminque ingestion service, one multipart request per
frame (POST /v1/screenshots), plus one POST /v1/heartbeat per cycle.

Runs as a short-lived process every 45 minutes via Task Scheduler.
Persistent cursor (sender_state.json) advances only on accepted frames —
at-least-once delivery; the server dedupes on (agent_id, captured_at).
Credentials are stored in Windows Credential Manager via keyring.
"""
import sys


def run() -> None:
    """Execute one send cycle."""
    from luminque.sender.sender import run_sender
    sys.exit(run_sender())
