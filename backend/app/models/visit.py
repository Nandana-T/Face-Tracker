"""Visit lifecycle (Phase 4). One *visit* is one confirmed presence episode of one persistent visitor.

Identifiers are never interchangeable:
  track_id   temporary, session-scoped trajectory (never stored here)
  visitor_id persistent facial identity (``visitors.id``)
  visit_id   ``visits.id`` - one presence episode; a visitor can have many, a visit may span several tracks

Exactly-once *logical* events are a database guarantee: ``UNIQUE(visit_id, slot)`` allows at most one
entry and one terminal (exit | interrupted) event per visit, ``UNIQUE(idempotency_key)`` makes a retried
write return the existing row. Files and ``events.log`` are side effects delivered after the commit
(see ``event_delivery_outbox``); nothing here claims atomicity across PostgreSQL and the filesystem.

Visitor erasure: ``visitor_id`` becomes NULL (ON DELETE SET NULL) and the evidence images are deleted;
the rows stay as an anonymous audit trail (timestamps, source, pseudonymous number).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

VISIT_STATES = ("ACTIVE", "TEMPORARILY_MISSING", "EXIT_PENDING", "CLOSED", "INTERRUPTED")
EVENT_TYPES = ("entry", "exit", "interrupted")
IMAGE_STATUSES = ("PENDING", "STORED", "UNAVAILABLE", "MISSING", "ERASED")
PERSISTENCE_STATUSES = ("PENDING", "DELIVERED", "DEAD")
EXIT_BASES = ("absence", "observation_ended", "interrupted")


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Visit(Base):
    __tablename__ = "visits"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in(VISIT_STATES)})", name="state_valid"),
        CheckConstraint(
            f"exit_basis IS NULL OR exit_basis IN ({_in(EXIT_BASES)})", name="exit_basis_valid"
        ),
        CheckConstraint("exit_time IS NULL OR exit_time >= entry_time", name="exit_after_entry"),
        UniqueConstraint("visit_key", name="uq_visits_visit_key"),
        Index("ix_visits_visitor_id", "visitor_id"),
        Index("ix_visits_session_id", "session_id"),
        Index("ix_visits_entry_time", "entry_time"),
        Index("ix_visits_state", "state"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # "<session>:<visitor>:<ordinal>" - the stable idempotency key; ``id`` is derived from it.
    visit_key: Mapped[str] = mapped_column(String(100), nullable=False)
    visitor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("visitors.id", ondelete="SET NULL")
    )
    visitor_number: Mapped[int | None] = mapped_column(Integer)  # pseudonymous snapshot
    session_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("processing_sessions.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_label: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    # entry_time / last_observed_time / exit_time are WALL-CLOCK (UTC) times of the first / last
    # defensible observation; *_video_ms is the position in the video. The exit is the LAST
    # observation, never an invented departure moment; ``exit_confirmed_at`` is when it was decided.
    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    entry_video_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    last_observed_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_video_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_video_ms: Mapped[int | None] = mapped_column(Integer)
    exit_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_basis: Mapped[str | None] = mapped_column(String(24))
    closure_reason: Mapped[str | None] = mapped_column(String(40))
    track_segments: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VisitEvent(Base):
    __tablename__ = "visit_events"
    __table_args__ = (
        CheckConstraint(f"event_type IN ({_in(EVENT_TYPES)})", name="type_valid"),
        CheckConstraint("slot IN ('entry', 'terminal')", name="slot_valid"),
        CheckConstraint("(slot = 'entry') = (event_type = 'entry')", name="slot_matches_type"),
        CheckConstraint(f"image_status IN ({_in(IMAGE_STATUSES)})", name="image_status_valid"),
        CheckConstraint(
            f"persistence_status IN ({_in(PERSISTENCE_STATUSES)})", name="persistence_status_valid"
        ),
        # At most one entry and one terminal event per visit - the exactly-once invariant.
        UniqueConstraint("visit_id", "slot", name="uq_visit_events_visit_slot"),
        UniqueConstraint("idempotency_key", name="uq_visit_events_idempotency_key"),
        Index("ix_visit_events_visitor_id", "visitor_id"),
        Index("ix_visit_events_event_time", "event_time"),
        Index("ix_visit_events_persistence_status", "persistence_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    visit_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("visits.id", ondelete="CASCADE"), nullable=False
    )
    visitor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("visitors.id", ondelete="SET NULL")
    )
    visitor_number: Mapped[int | None] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(12), nullable=False)
    slot: Mapped[str] = mapped_column(String(12), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    # Hash of the immutable business fields: an identical retry is a duplicate, a different payload
    # under the same key is a CONFLICT (reported, never silently accepted).
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    video_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    image_status: Mapped[str] = mapped_column(String(12), nullable=False)
    # Relative path inside the evidence root (entries/YYYY-MM-DD/<event_id>.jpg); never served statically.
    image_path: Mapped[str | None] = mapped_column(String(120))
    image_sha256: Mapped[str | None] = mapped_column(String(64))
    # entry: the best observation so far; exit: the LAST GOOD observation (not a fresh capture)
    image_kind: Mapped[str | None] = mapped_column(String(24))
    image_video_ms: Mapped[int | None] = mapped_column(Integer)
    persistence_status: Mapped[str] = mapped_column(String(12), nullable=False, default="PENDING")


class EventDeliveryOutbox(Base):
    """Durable to-do list for side effects of a committed event (currently: the events.log line)."""

    __tablename__ = "event_delivery_outbox"
    __table_args__ = (
        CheckConstraint(f"status IN ({_in(PERSISTENCE_STATUSES)})", name="status_valid"),
        UniqueConstraint("event_id", "target", name="uq_event_delivery_outbox_event_target"),
        Index("ix_event_delivery_outbox_status_retry", "status", "next_retry_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("visit_events.id", ondelete="CASCADE"), nullable=False
    )
    target: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(200))
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
