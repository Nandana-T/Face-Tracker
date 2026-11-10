"""Persistence for processing sessions (synchronous SQLAlchemy; call via a worker thread)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models.processing_session import ACTIVE_STATES, ProcessingSession

TERMINAL_STATES = ("STOPPED", "COMPLETED", "ERROR")

# Allowed lifecycle transitions (IDLE is the absence of a session and is never stored).
TRANSITIONS: dict[str, frozenset[str]] = {
    "STARTING": frozenset({"RUNNING", "STOPPING", "STOPPED", "COMPLETED", "ERROR"}),
    "RUNNING": frozenset({"STOPPING", "STOPPED", "COMPLETED", "ERROR"}),
    "STOPPING": frozenset({"STOPPED", "COMPLETED", "ERROR"}),
    "STOPPED": frozenset(),
    "COMPLETED": frozenset(),
    "ERROR": frozenset(),
}


def can_transition(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, frozenset())


class ActiveSessionExists(Exception):
    pass


class InvalidTransition(Exception):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


class SessionStore:
    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def create_active(
        self,
        source_id: str,
        label: str,
        source_type: str,
        worker_id: str | None,
        settings_snapshot: dict[str, Any],
    ) -> ProcessingSession:
        row = ProcessingSession(
            id=uuid.uuid4().hex,
            source_id=source_id,
            source_label=label[:80],
            source_type=source_type,
            state="STARTING",
            active_slot=1,
            worker_id=worker_id,
            frames_read=0,
            frames_processed=0,
            frames_skipped=0,
            frames_dropped=0,
            corrupt_frames=0,
            detections_total=0,
            max_faces_in_frame=0,
            settings_snapshot=settings_snapshot,
        )
        with self._factory() as db:
            db.add(row)
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                raise ActiveSessionExists from exc
            db.refresh(row)
            db.expunge(row)
            return row

    def get(self, session_id: str) -> ProcessingSession | None:
        with self._factory() as db:
            row = db.get(ProcessingSession, session_id)
            if row is not None:
                db.expunge(row)
            return row

    def active(self) -> ProcessingSession | None:
        with self._factory() as db:
            row = db.scalars(
                select(ProcessingSession).where(ProcessingSession.state.in_(ACTIVE_STATES))
            ).first()
            if row is not None:
                db.expunge(row)
            return row

    def recent(self, limit: int) -> list[ProcessingSession]:
        with self._factory() as db:
            rows = list(
                db.scalars(
                    select(ProcessingSession)
                    .order_by(ProcessingSession.created_at.desc())
                    .limit(limit)
                )
            )
            db.expunge_all()
            return rows

    def transition(
        self,
        session_id: str,
        new_state: str,
        *,
        stop_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> ProcessingSession:
        with self._factory() as db:
            row = db.get(ProcessingSession, session_id)
            if row is None:
                raise KeyError(session_id)
            if not can_transition(row.state, new_state):
                raise InvalidTransition(f"{row.state} -> {new_state}")
            row.state = new_state
            if new_state == "RUNNING" and row.started_at is None:
                row.started_at = _now()
            if new_state in TERMINAL_STATES:
                row.active_slot = None
                row.ended_at = _now()
            if stop_reason is not None:
                row.stop_reason = stop_reason
            if error_code is not None:
                row.error_code = error_code[:60]
                row.error_message = (error_message or "")[:300]
            if stats:
                _apply_stats(row, stats)
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row

    def save_stats(self, session_id: str, stats: dict[str, Any]) -> None:
        with self._factory() as db:
            row = db.get(ProcessingSession, session_id)
            if row is not None and row.state in ACTIVE_STATES:
                _apply_stats(row, stats)
                db.commit()

    def fail_orphans(self, code: str, message: str) -> int:
        """Mark every non-terminal session ERROR (start-up reconciliation and shutdown)."""
        with self._factory() as db:
            result = db.execute(
                update(ProcessingSession)
                .where(ProcessingSession.state.in_(ACTIVE_STATES))
                .values(
                    state="ERROR",
                    active_slot=None,
                    ended_at=_now(),
                    error_code=code,
                    error_message=message,
                )
            )
            db.commit()
            return int(getattr(result, "rowcount", 0) or 0)


def _apply_stats(row: ProcessingSession, stats: dict[str, Any]) -> None:
    row.summary = {k: v for k, v in stats.items() if isinstance(v, int | float)}
    for key in (
        "frames_read",
        "frames_processed",
        "frames_skipped",
        "frames_dropped",
        "corrupt_frames",
        "detections_total",
        "max_faces_in_frame",
    ):
        if key in stats:
            setattr(row, key, int(stats[key]))
    if stats.get("avg_inference_ms") is not None:
        row.avg_inference_ms = float(stats["avg_inference_ms"])
    if stats.get("processing_fps") is not None:
        row.avg_processing_fps = float(stats["processing_fps"])
