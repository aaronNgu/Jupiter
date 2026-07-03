"""SQLAlchemy models for reading the capture DB.

Mirrors the column layout of openadapt-capture's models
(openadapt_capture/db/models.py) for the three tables the sender touches, so
DBs written by either the legacy capturer or captureV2 read identically.
Definitions must stay in lockstep with luminque/captureV2/schema.py.

Read-only from the sender's perspective except for nullifying screenshot
blobs (cleanup + retention cap). No relationships — the sender resolves
foreign keys with explicit queries.
"""

import sqlalchemy as sa
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ActionEvent(Base):
    __tablename__ = "action_event"

    id = sa.Column(sa.Integer, primary_key=True)
    name = sa.Column(sa.String)
    timestamp = sa.Column(sa.Float)
    recording_timestamp = sa.Column(sa.Float)
    recording_id = sa.Column(sa.Integer)
    screenshot_timestamp = sa.Column(sa.Float)
    screenshot_id = sa.Column(sa.Integer)
    window_event_timestamp = sa.Column(sa.Float)
    window_event_id = sa.Column(sa.Integer)
    browser_event_timestamp = sa.Column(sa.Float)
    browser_event_id = sa.Column(sa.Integer)
    mouse_x = sa.Column(sa.Float)
    mouse_y = sa.Column(sa.Float)
    mouse_dx = sa.Column(sa.Float)
    mouse_dy = sa.Column(sa.Float)
    active_segment_description = sa.Column(sa.String)
    _available_segment_descriptions = sa.Column(
        "available_segment_descriptions",
        sa.String,
    )
    mouse_button_name = sa.Column(sa.String)
    mouse_pressed = sa.Column(sa.Boolean)
    key_name = sa.Column(sa.String)
    key_char = sa.Column(sa.String)
    key_vk = sa.Column(sa.String)
    canonical_key_name = sa.Column(sa.String)
    canonical_key_char = sa.Column(sa.String)
    canonical_key_vk = sa.Column(sa.String)
    parent_id = sa.Column(sa.Integer)
    element_state = sa.Column(sa.JSON)
    disabled = sa.Column(sa.Boolean)


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
