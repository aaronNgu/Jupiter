"""Contract tests against the compose Postgres + MinIO."""

import base64
import io
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from luminque_ingestion import cli, storage
from luminque_ingestion.auth import sha256_hex
from luminque_ingestion.db import get_engine
from luminque_ingestion.models import Agent, Screenshot, Tenant

# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
CAPTURED_AT = "2026-07-04T12:00:00Z"


def make_tenant(name: str = "Test Tenant") -> tuple[uuid.UUID, str]:
    token = f"enroll-{uuid.uuid4()}"
    with Session(get_engine()) as db:
        tenant = Tenant(
            name=name, enrollment_token=token, created_at=datetime.now(timezone.utc)
        )
        db.add(tenant)
        db.commit()
        return tenant.id, token


def enroll(client, enrollment_token: str) -> tuple[str, str]:
    resp = client.post(
        "/v1/enroll",
        json={
            "enrollment_token": enrollment_token,
            "hostname": "DESKTOP-TEST",
            "platform": "windows",
            "os_version": "10.0.22631",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    return body["agent_id"], body["auth_token"]


def upload(client, auth_token: str, captured_at: str = CAPTURED_AT, data: bytes = PNG, **form):
    return client.post(
        "/v1/screenshots",
        headers={"X-Device-Token": auth_token},
        files={"file": ("shot.png", io.BytesIO(data), "image/png")},
        data={"captured_at": captured_at, **form},
    )


def test_enroll_happy_path(client):
    tenant_id, enrollment_token = make_tenant()
    agent_id, auth_token = enroll(client, enrollment_token)

    with Session(get_engine()) as db:
        agent = db.get(Agent, uuid.UUID(agent_id))
        assert agent is not None
        assert agent.tenant_id == tenant_id
        assert agent.hostname == "DESKTOP-TEST"
        assert agent.platform == "windows"
        assert agent.os_version == "10.0.22631"
        assert agent.status == "active"
        # only the sha256 is stored, never the plaintext token
        assert agent.token_hash == sha256_hex(auth_token)
        assert auth_token not in (agent.token_hash,)


def test_enroll_bad_token_401(client):
    resp = client.post(
        "/v1/enroll",
        json={
            "enrollment_token": "no-such-token",
            "hostname": "h",
            "platform": "windows",
            "os_version": "10",
        },
    )
    assert resp.status_code == 401


def test_enroll_missing_fields_422(client):
    resp = client.post("/v1/enroll", json={"enrollment_token": "x"})
    assert resp.status_code == 422


def test_screenshot_upload_happy_path(client):
    tenant_id, enrollment_token = make_tenant()
    agent_id, auth_token = enroll(client, enrollment_token)

    resp = upload(client, auth_token, window_title="Invoice.xlsx — Excel", app_name="EXCEL.EXE")
    assert resp.status_code == 201, resp.text
    screenshot_id = resp.json()["id"]

    expected_key = f"{tenant_id}/{agent_id}/2026-07-04/2026-07-04T12:00:00Z.png"
    with Session(get_engine()) as db:
        row = db.get(Screenshot, uuid.UUID(screenshot_id))
        assert row.agent_id == uuid.UUID(agent_id)
        assert row.tenant_id == tenant_id
        assert row.captured_at == datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
        assert row.received_at is not None
        assert row.s3_key == expected_key
        assert row.window_title == "Invoice.xlsx — Excel"
        assert row.app_name == "EXCEL.EXE"
        assert row.size_bytes == len(PNG)
        # upload bumps last_seen_at
        assert db.get(Agent, uuid.UUID(agent_id)).last_seen_at is not None

    # S3 object exists under the documented key layout and matches the DB row
    assert storage.get(expected_key) == PNG
    assert storage.list(f"{tenant_id}/{agent_id}/") == [expected_key]


def test_duplicate_returns_200_and_stores_nothing_new(client):
    tenant_id, enrollment_token = make_tenant()
    agent_id, auth_token = enroll(client, enrollment_token)

    first = upload(client, auth_token)
    assert first.status_code == 201
    dup = upload(client, auth_token)
    assert dup.status_code == 200
    assert dup.json()["id"] == first.json()["id"]

    with Session(get_engine()) as db:
        count = len(
            db.scalars(
                select(Screenshot).where(Screenshot.agent_id == uuid.UUID(agent_id))
            ).all()
        )
    assert count == 1
    assert len(storage.list(f"{tenant_id}/{agent_id}/")) == 1


def test_disabled_agent_401(client):
    _, enrollment_token = make_tenant()
    agent_id, auth_token = enroll(client, enrollment_token)

    with Session(get_engine()) as db:
        db.get(Agent, uuid.UUID(agent_id)).status = "disabled"
        db.commit()

    assert upload(client, auth_token).status_code == 401
    resp = client.post("/v1/heartbeat", headers={"X-Device-Token": auth_token})
    assert resp.status_code == 401


def test_unknown_or_missing_token_401(client):
    assert upload(client, "bogus-token").status_code == 401
    assert client.post("/v1/heartbeat", headers={"X-Device-Token": "bogus"}).status_code == 401
    assert client.post("/v1/heartbeat").status_code == 401


def test_heartbeat_bumps_last_seen(client):
    _, enrollment_token = make_tenant()
    agent_id, auth_token = enroll(client, enrollment_token)

    with Session(get_engine()) as db:
        assert db.get(Agent, uuid.UUID(agent_id)).last_seen_at is None

    resp = client.post("/v1/heartbeat", headers={"X-Device-Token": auth_token})
    assert resp.status_code == 204
    assert resp.content == b""

    with Session(get_engine()) as db:
        first_seen = db.get(Agent, uuid.UUID(agent_id)).last_seen_at
    assert first_seen is not None

    resp = client.post("/v1/heartbeat", headers={"X-Device-Token": auth_token})
    assert resp.status_code == 204
    with Session(get_engine()) as db:
        assert db.get(Agent, uuid.UUID(agent_id)).last_seen_at > first_seen


def test_oversized_upload_413(client, monkeypatch):
    tenant_id, enrollment_token = make_tenant()
    agent_id, auth_token = enroll(client, enrollment_token)

    monkeypatch.setenv("MAX_UPLOAD_BYTES", "100")
    resp = upload(client, auth_token, data=b"x" * 200)
    assert resp.status_code == 413

    with Session(get_engine()) as db:
        rows = db.scalars(
            select(Screenshot).where(Screenshot.agent_id == uuid.UUID(agent_id))
        ).all()
    assert rows == []
    assert storage.list(f"{tenant_id}/{agent_id}/") == []


def test_cli_creates_enrollable_tenant(client, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["luminque-create-tenant", "Acme Corp"])
    cli.main()
    out = capsys.readouterr().out
    enrollment_token = next(
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("enrollment_token:")
    )
    agent_id, auth_token = enroll(client, enrollment_token)
    assert uuid.UUID(agent_id)


def test_healthz_is_unauthenticated_liveness(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
