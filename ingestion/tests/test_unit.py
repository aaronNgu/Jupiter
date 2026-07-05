"""Unit tests — no Postgres/MinIO required."""

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from luminque_ingestion.auth import sha256_hex
from luminque_ingestion.app import parse_captured_at, screenshot_key


def test_sha256_hex_matches_stdlib():
    assert sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()


def test_screenshot_key_layout():
    tenant_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    agent_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    captured = datetime(2026, 7, 4, 12, 34, 56, tzinfo=timezone.utc)
    assert screenshot_key(tenant_id, agent_id, captured) == (
        "11111111-1111-1111-1111-111111111111/"
        "22222222-2222-2222-2222-222222222222/"
        "2026-07-04/2026-07-04T12:34:56Z.png"
    )


def test_parse_captured_at_accepts_z_suffix():
    dt = parse_captured_at("2026-07-04T12:34:56Z")
    assert dt == datetime(2026, 7, 4, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_captured_at_naive_treated_as_utc():
    dt = parse_captured_at("2026-07-04T12:34:56")
    assert dt.tzinfo == timezone.utc


def test_parse_captured_at_normalizes_offsets_to_utc():
    dt = parse_captured_at("2026-07-04T14:34:56+02:00")
    assert dt == datetime(2026, 7, 4, 12, 34, 56, tzinfo=timezone.utc)


def test_parse_captured_at_rejects_garbage():
    with pytest.raises(HTTPException) as exc:
        parse_captured_at("not-a-timestamp")
    assert exc.value.status_code == 422


def test_boto3_confined_to_storage_module():
    import ast
    import pathlib

    pkg = pathlib.Path(__file__).parent.parent / "luminque_ingestion"
    offenders = []
    for path in pkg.rglob("*.py"):
        if path.name == "storage.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "boto3" or n.startswith("boto3.") for n in names):
                offenders.append(str(path))
    assert offenders == []
