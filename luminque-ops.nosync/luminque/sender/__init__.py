"""
luminque.sender — reads the capture DB, (Phase 2: scrubs PII), and ships data
to the Luminque cloud ingest endpoint.

Runs as a short-lived process every 45 minutes via Task Scheduler.
Uses a persistent cursor (sender_state.json) for exactly-once delivery.
Credentials are stored in Windows Credential Manager via keyring.
"""
import sys


def run() -> None:
    """Execute one send cycle."""
    from luminque.sender.sender import run_sender
    sys.exit(run_sender())
