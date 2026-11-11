"""Visitor / recognition API schemas.

Deliberately absent from every response: embedding vectors, image file paths and any biometric
template. ``id`` is the persistent visitor identifier; ``track_id`` only ever appears inside an
association together with its processing ``session_id`` (it is temporary and session-scoped).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class VisitorOut(BaseModel):
    id: str
    number: int
    label: str  # "V00124" - the short public identifier
    status: str
    first_seen_at: datetime
    last_seen_at: datetime
    recognition_count: int  # tracks that were recognised as this visitor (NOT visits)
    has_image: bool
    model_version: str


class VisitorListResponse(BaseModel):
    items: list[VisitorOut]
    total: int
    limit: int
    offset: int


class AssociationOut(BaseModel):
    session_id: str
    source_label: str | None
    track_id: int  # temporary, meaningful only inside `session_id`
    state: str
    best_similarity: float | None
    observations: int
    conflicts: int
    registered_in_session: bool
    first_seen_at: datetime
    last_seen_at: datetime


class VisitorDetail(VisitorOut):
    first_session_id: str | None
    image_quality: float | None
    sessions_count: int
    tracks_count: int
    associations: list[AssociationOut]


class RecognitionStatus(BaseModel):
    enabled: bool
    model: str
    model_version: str | None
    worker_available: bool | None
    worker_detail: str | None
    registered_visitors: int
    indexed_embeddings: int


class RecognitionStats(BaseModel):
    registered_visitors: int
    associations: int
    runtime: dict[str, Any]  # counters of this API process (never embeddings)
    thresholds: dict[str, float]
