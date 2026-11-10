"""One row per video-processing run (a "session").

Stores lifecycle + summary counters only - never frames, images or per-frame detections.
``active_slot`` is 1 while the session is in an active state and NULL otherwise; its UNIQUE
constraint makes "at most one active session" a database guarantee (NULLs are distinct), so two
competing start requests cannot both succeed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    SmallInteger,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SESSION_STATES = ("STARTING", "RUNNING", "STOPPING", "STOPPED", "COMPLETED", "ERROR")
ACTIVE_STATES = ("STARTING", "RUNNING", "STOPPING")
_STATE_LIST = ", ".join(f"'{s}'" for s in SESSION_STATES)


class ProcessingSession(Base):
    __tablename__ = "processing_sessions"
    __table_args__ = (
        CheckConstraint(f"state IN ({_STATE_LIST})", name="state_valid"),
        CheckConstraint("active_slot IS NULL OR active_slot = 1", name="active_slot_valid"),
        Index("ix_processing_sessions_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_label: Mapped[str] = mapped_column(String(80), nullable=False)
    source_type: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    active_slot: Mapped[int | None] = mapped_column(SmallInteger, unique=True, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(40))
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(String(300))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    frames_read: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frames_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frames_skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    frames_dropped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    corrupt_frames: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Sum of faces over processed frames. NOT a count of distinct people.
    detections_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_faces_in_frame: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    avg_inference_ms: Mapped[float | None] = mapped_column(Float)
    avg_processing_fps: Mapped[float | None] = mapped_column(Float)
    # Last stats snapshot from the worker (counters only; never frames or per-frame boxes).
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    settings_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
