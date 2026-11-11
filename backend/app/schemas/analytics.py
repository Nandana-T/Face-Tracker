"""Analytics API schemas. Definitions live in ``app/services/analytics.py`` and ``docs/phase-5-report.md``.

Nothing here is a count of physical people. Never present: embeddings, storage paths, credentials.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ScopeOut(BaseModel):
    since: str | None
    until: str | None
    source_id: str | None
    session_id: str | None


class TerminalByBasis(BaseModel):
    absence: int
    observation_ended: int
    interrupted: int


class OverviewOut(BaseModel):
    scope: ScopeOut
    registered_visitors: int  # global; ignores the scope
    unique_visitors_in_scope: int
    returning_visitors_in_scope: int
    new_visitors_in_scope: int
    visits: int
    visits_without_identity: int  # visits of erased visitors (kept, de-identified)
    entry_events: int
    terminal_events: int
    terminal_by_basis: TerminalByBasis
    active_visits: int  # open visits of LIVE sessions; ignores the date range
    active_visits_temporarily_missing: int
    stale_open_visits: int  # open rows of sessions that are not live: NOT present
    unresolved_tracks: int  # historical diagnostic; never extra people
    generated_at: str
    last_event_recorded_at: str | None
    definitions: dict[str, str]


class TimelinePoint(BaseModel):
    bucket_start: datetime  # UTC
    visits: int
    entries: int
    unique_visitors: int  # per bucket; not additive across buckets
    exits_absence: int
    exits_observation_ended: int
    interrupted: int


class TimelineOut(BaseModel):
    scope: ScopeOut
    bucket: str
    timezone: str
    truncated: bool
    points: list[TimelinePoint]
    note: str


class SourceStat(BaseModel):
    source_id: str
    source_label: str | None
    visits: int
    unique_visitors: int
    sessions: int
    last_entry_at: str | None


class SourcesOut(BaseModel):
    scope: ScopeOut
    items: list[SourceStat]


class SessionStat(BaseModel):
    session_id: str
    source_id: str
    source_label: str
    state: str
    live: bool
    started_at: str
    ended_at: str | None
    visits: int
    unique_visitors: int
    interrupted_visits: int
    visitors_registered_here: int


class SessionsOut(BaseModel):
    scope: ScopeOut
    total: int
    limit: int
    offset: int
    note: str
    items: list[SessionStat]


class CoverageOut(BaseModel):
    scope: ScopeOut
    sessions_in_scope: int
    confirmed_tracks: int
    tracks_with_recognition_observations: int
    tracks_resolved: int
    tracks_unresolved: int
    tracks_by_state: dict[str, int]
    unresolved_breakdown: dict[str, int]
    resolved_share_of_evaluated_tracks: float | None
    face_crops_analysed: int
    face_crops_usable: int
    face_crops_rejected_by_quality_gate: int
    face_crops_dropped_busy: int
    recognition_errors: int
    disclaimer: str


class ReconcileCheck(BaseModel):
    name: str
    ok: bool
    expected: Any
    actual: Any
    detail: str
    severity: str


class ReconciliationOut(BaseModel):
    consistent: bool
    errors: int
    warnings: int
    checks: list[ReconcileCheck]
    mode: str
    duration_ms: float
    generated_at: str
