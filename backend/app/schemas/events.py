"""Event / visit API schemas.

Never present: embeddings, storage paths, credentials. ``visitor`` is null for an erased visitor.
Identifiers are distinct: ``visitor.id`` (person), ``visit_id`` (one presence episode), ``id`` (one event).
There is deliberately NO track id and NO unique-visitor count here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

EventType = Literal["entry", "exit", "interrupted"]


class VisitorRef(BaseModel):
    id: str
    number: int | None
    label: str


class EventOut(BaseModel):
    id: str
    visit_id: str
    type: EventType
    visitor: VisitorRef | None
    session_id: str
    source_id: str
    source_label: str
    event_time: datetime  # wall-clock time of the (first / last) defensible observation
    video_ms: int  # position in the video
    recorded_at: datetime  # when the event was confirmed and stored
    image_status: str  # STORED | PENDING | UNAVAILABLE | MISSING | ERASED
    has_image: bool
    image_kind: str | None  # observation | last_good_observation | entry_representative
    persistence_status: str  # PENDING | DELIVERED | DEAD
    exit_basis: str | None  # absence | observation_ended | interrupted (terminal events only)
    closure_reason: str | None


class EventListResponse(BaseModel):
    items: list[EventOut]
    total: int
    limit: int
    offset: int


class VisitOut(BaseModel):
    id: str
    visitor: VisitorRef | None
    session_id: str
    source_id: str
    source_label: str
    state: str
    entry_time: datetime
    last_observed_time: datetime
    exit_time: datetime | None
    exit_confirmed_at: datetime | None
    exit_basis: str | None
    closure_reason: str | None
    track_segments: int
    # Observed presence in VIDEO time; only for a visit that closed normally - never for an open or
    # interrupted one (their end is unknown), and "observation_ended" means "until observation stopped".
    duration_seconds: float | None
    integrity: Literal["open", "complete", "pending", "evidence_missing", "delivery_failed"]


class VisitDetail(VisitOut):
    events: list[EventOut]


class VisitListResponse(BaseModel):
    items: list[VisitOut]
    total: int
    limit: int
    offset: int


class EventsHealth(BaseModel):
    enabled: bool
    events_by_persistence: dict[str, int]
    events_by_image_status: dict[str, int]
    open_visits: int
    pending_image_retries: int
    counters: dict[str, int]
    latency: dict[str, dict[str, float]]
    last_reconcile: dict[str, Any] | None
    runtime: dict[str, Any]
    events_log_present: bool
    events_log_bytes: int


class LogVerification(BaseModel):
    database_events: int
    logged_events: int
    log_lines: int
    malformed_lines: int
    missing_in_log: list[str]
    missing_in_log_count: int
    logged_not_in_db: list[str]
    logged_not_in_db_count: int
    redelivered_count: int
    unexplained_duplicates: list[str]
    unexplained_duplicates_count: int
    pending_delivery_count: int
    dead_count: int
    consistent: bool


class ReconcileResult(BaseModel):
    result: dict[str, int]
