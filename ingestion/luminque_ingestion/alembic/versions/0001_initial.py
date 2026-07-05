"""tenant / agent / screenshot

Revision ID: 0001
Revises:
Create Date: 2026-07-04

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        # reusable, tenant-scoped
        sa.Column("enrollment_token", sa.Text(), nullable=False, unique=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "agent",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.Uuid(), sa.ForeignKey("tenant.id"), nullable=False),
        # sha256 of the device token
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("os_version", sa.Text(), nullable=False),
        # active | disabled (admin kill switch; online/offline derives from last_seen_at)
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_table(
        "screenshot",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("agent_id", sa.Uuid(), sa.ForeignKey("agent.id"), nullable=False),
        # denormalized from agent at insert
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("s3_key", sa.Text(), nullable=False),
        sa.Column("window_title", sa.Text(), nullable=True),
        sa.Column("app_name", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        # idempotency for at-least-once delivery
        sa.UniqueConstraint("agent_id", "captured_at", name="uq_screenshot_agent_captured"),
    )
    # discovery's query pattern
    op.create_index(
        "ix_screenshot_tenant_captured", "screenshot", ["tenant_id", "captured_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_screenshot_tenant_captured", table_name="screenshot")
    op.drop_table("screenshot")
    op.drop_table("agent")
    op.drop_table("tenant")
