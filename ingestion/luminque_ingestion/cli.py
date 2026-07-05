"""Dev CLI: create a tenant with an enrollment token. There is no admin API —
this is how a tenant row is born.

Usage: uv run --env-file .env luminque-create-tenant "<tenant name>"
"""

import secrets
import sys
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from luminque_ingestion.db import get_engine
from luminque_ingestion.migrations import run_migrations
from luminque_ingestion.models import Tenant


def main() -> None:
    if len(sys.argv) != 2:
        print('usage: luminque-create-tenant "<tenant name>"', file=sys.stderr)
        raise SystemExit(2)
    name = sys.argv[1]

    run_migrations()  # safe if the app has never started yet

    enrollment_token = secrets.token_urlsafe(32)
    tenant = Tenant(
        name=name,
        enrollment_token=enrollment_token,
        created_at=datetime.now(timezone.utc),
    )
    with Session(get_engine()) as db:
        db.add(tenant)
        db.commit()
        print(f"tenant_id:        {tenant.id}")
    print(f"enrollment_token: {enrollment_token}")


if __name__ == "__main__":
    main()
