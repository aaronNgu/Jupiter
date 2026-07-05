"""Contract tests run against the compose Postgres + MinIO (docker compose up -d).

They use a dedicated `luminque_test` database (dropped and recreated per run)
and the compose `luminque-dev` bucket; every test enrolls fresh agents under
fresh tenants, so bucket state never collides across runs.
"""

import os
import socket

# Env defaults must be set before the app package is imported anywhere.
_TEST_ENV = {
    "DATABASE_URL": "postgresql+psycopg://postgres:dev@localhost:5432/luminque_test",
    "S3_ENDPOINT_URL": "http://localhost:9000",
    "S3_BUCKET": "luminque-dev",
    "AWS_ACCESS_KEY_ID": "minioadmin",
    "AWS_SECRET_ACCESS_KEY": "minioadmin",
    "AWS_DEFAULT_REGION": "us-east-1",
}
for _k, _v in _TEST_ENV.items():
    os.environ.setdefault(_k, _v)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from luminque_ingestion import db as db_module
from luminque_ingestion.app import app

ADMIN_URL = "postgresql+psycopg://postgres:dev@localhost:5432/postgres"


def _reachable(port: int) -> bool:
    try:
        with socket.create_connection(("localhost", port), timeout=1):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def compose_services():
    if not (_reachable(5432) and _reachable(9000)):
        pytest.skip("compose Postgres+MinIO not reachable — run `docker compose up -d`")


@pytest.fixture(scope="session")
def client(compose_services):
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS luminque_test WITH (FORCE)"))
        conn.execute(text("CREATE DATABASE luminque_test"))
    admin.dispose()

    db_module.dispose_engine()
    # Entering the context runs the lifespan: migrations under the advisory lock.
    with TestClient(app) as c:
        yield c
    db_module.dispose_engine()
