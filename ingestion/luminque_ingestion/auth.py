import hashlib
from typing import Annotated

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from luminque_ingestion.db import get_db
from luminque_ingestion.models import Agent


def sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def require_agent(
    x_device_token: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> Agent:
    """Resolve X-Device-Token to an active agent, or 401.

    Identity comes from the token: agent_id and tenant_id are derived here
    on every request, never read from request bodies.
    """
    if not x_device_token:
        raise HTTPException(status_code=401, detail="missing device token")
    agent = db.scalar(select(Agent).where(Agent.token_hash == sha256_hex(x_device_token)))
    if agent is None or agent.status != "active":
        raise HTTPException(status_code=401, detail="unknown or disabled device token")
    return agent
