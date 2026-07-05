import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {datetime: TIMESTAMP(timezone=True)}


class Tenant(Base):
    __tablename__ = "tenant"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text)
    enrollment_token: Mapped[str] = mapped_column(Text, unique=True)  # reusable, tenant-scoped
    created_at: Mapped[datetime]


class Agent(Base):
    __tablename__ = "agent"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)  # sha256 of the device token
    hostname: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    os_version: Mapped[str] = mapped_column(Text)
    # active | disabled (admin kill switch; online/offline derives from last_seen_at)
    status: Mapped[str] = mapped_column(Text, default="active", server_default="active")
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime]


class Screenshot(Base):
    __tablename__ = "screenshot"
    __table_args__ = (
        UniqueConstraint("agent_id", "captured_at", name="uq_screenshot_agent_captured"),
        Index("ix_screenshot_tenant_captured", "tenant_id", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent.id"))
    tenant_id: Mapped[uuid.UUID]  # denormalized from agent at insert
    captured_at: Mapped[datetime]  # client capture time
    received_at: Mapped[datetime]  # server receipt time (upload-lag signal)
    s3_key: Mapped[str] = mapped_column(Text)  # key only; bucket/region live in config
    window_title: Mapped[str | None] = mapped_column(Text)
    app_name: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int]
