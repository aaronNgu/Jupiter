import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from luminque_ingestion import storage
from luminque_ingestion.auth import require_agent, sha256_hex
from luminque_ingestion.config import get_settings
from luminque_ingestion.db import get_db
from luminque_ingestion.migrations import run_migrations
from luminque_ingestion.models import Agent, Screenshot, Tenant


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations()
    yield


app = FastAPI(title="luminque-ingestion", lifespan=lifespan)


@app.get("/healthz")
def healthz():
    # ALB target-group health check: liveness only, no DB or S3 calls.
    return {"status": "ok"}


def parse_captured_at(raw: str) -> datetime:
    """Parse the client's ISO-8601 UTC capture time; naive input is taken as UTC."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="captured_at is not ISO-8601")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def screenshot_key(tenant_id: uuid.UUID, agent_id: uuid.UUID, captured_at: datetime) -> str:
    """{tenant_id}/{agent_id}/{YYYY-MM-DD}/{captured_at_iso}.png — prefixed so
    manual discovery runs are a single sync on an agent/date prefix."""
    iso = captured_at.isoformat().replace("+00:00", "Z")
    return f"{tenant_id}/{agent_id}/{captured_at:%Y-%m-%d}/{iso}.png"


class EnrollRequest(BaseModel):
    enrollment_token: str
    hostname: str
    platform: str
    os_version: str


@app.post("/v1/enroll", status_code=201)
def enroll(body: EnrollRequest, db: Session = Depends(get_db)):
    tenant = db.scalar(
        select(Tenant).where(Tenant.enrollment_token == body.enrollment_token)
    )
    if tenant is None:
        raise HTTPException(status_code=401, detail="unknown enrollment token")

    # Token returned in plaintext exactly once; only its sha256 is stored.
    auth_token = secrets.token_urlsafe(32)
    agent = Agent(
        tenant_id=tenant.id,
        token_hash=sha256_hex(auth_token),
        hostname=body.hostname,
        platform=body.platform,
        os_version=body.os_version,
        created_at=datetime.now(timezone.utc),
    )
    db.add(agent)
    db.commit()
    return {"agent_id": str(agent.id), "auth_token": auth_token}


@app.post("/v1/screenshots", status_code=201)
def upload_screenshot(
    file: UploadFile = File(...),
    captured_at: str = Form(...),
    window_title: str | None = Form(None),
    app_name: str | None = Form(None),
    agent: Agent = Depends(require_agent),
    db: Session = Depends(get_db),
):
    captured = parse_captured_at(captured_at)
    max_bytes = get_settings().max_upload_bytes
    data = file.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail="file too large")

    now = datetime.now(timezone.utc)

    def duplicate_response() -> JSONResponse:
        existing_id = db.scalar(
            select(Screenshot.id).where(
                Screenshot.agent_id == agent.id, Screenshot.captured_at == captured
            )
        )
        db.get(Agent, agent.id).last_seen_at = now
        db.commit()
        return JSONResponse(status_code=200, content={"id": str(existing_id)})

    # At-least-once delivery: retries resend, so duplicates are success (200),
    # never an error. Cheap precheck first; the unique index catches races.
    if db.scalar(
        select(Screenshot.id).where(
            Screenshot.agent_id == agent.id, Screenshot.captured_at == captured
        )
    ):
        return duplicate_response()

    # S3 put first, then DB insert: an orphaned S3 object on crash is
    # harmless; a row pointing at nothing is not.
    key = screenshot_key(agent.tenant_id, agent.id, captured)
    storage.put(key, data)

    row = Screenshot(
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        captured_at=captured,
        received_at=now,
        s3_key=key,
        window_title=window_title,
        app_name=app_name,
        size_bytes=len(data),
    )
    db.add(row)
    agent.last_seen_at = now
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return duplicate_response()
    return {"id": str(row.id)}


@app.post("/v1/heartbeat", status_code=204)
def heartbeat(agent: Agent = Depends(require_agent), db: Session = Depends(get_db)):
    agent.last_seen_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)
