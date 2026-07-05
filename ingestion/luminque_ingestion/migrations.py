"""Run Alembic migrations at startup, serialized by a Postgres advisory lock
so two starting tasks can't race."""

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from luminque_ingestion.config import get_settings
from luminque_ingestion.db import get_engine

ADVISORY_LOCK_KEY = 0x4C554D49  # "LUMI"


def run_migrations() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        try:
            cfg = Config()
            cfg.set_main_option("script_location", "luminque_ingestion:alembic")
            cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
            command.upgrade(cfg, "head")
        finally:
            conn.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ADVISORY_LOCK_KEY}
            )
