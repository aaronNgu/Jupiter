"""SQLAlchemy models for reading the capture DB.

Mirrors the column layout of openadapt-capture's models
(openadapt_capture/db/models.py) for the two tables the sender touches, so
DBs written by either the legacy capturer or captureV2 read identically.
Definitions must stay in lockstep with luminque/captureV2/schema.py.

Read-only from the sender's perspective except for nullifying screenshot
blobs (cleanup + retention cap). No relationships — the sender resolves
the screenshot→window_event linkage with an explicit timestamp query
(db.window_for_screenshot).
"""

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class WindowEvent(Base):
    __tablename__ = "window_event"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Float)
    recording_id = sa.Column(sa.Integer)
    timestamp = sa.Column(sa.Float)
    state = sa.Column(sa.JSON)
    title = sa.Column(sa.String)
    left = sa.Column(sa.Integer)
    top = sa.Column(sa.Integer)
    width = sa.Column(sa.Integer)
    height = sa.Column(sa.Integer)
    window_id = sa.Column(sa.String)


class Screenshot(Base):
    __tablename__ = "screenshot"

    id = sa.Column(sa.Integer, primary_key=True)
    recording_timestamp = sa.Column(sa.Float)
    recording_id = sa.Column(sa.Integer)
    timestamp = sa.Column(sa.Float)
    png_data = sa.Column(sa.LargeBinary)
    png_diff_data = sa.Column(sa.LargeBinary, nullable=True)
    png_diff_mask_data = sa.Column(sa.LargeBinary, nullable=True)
