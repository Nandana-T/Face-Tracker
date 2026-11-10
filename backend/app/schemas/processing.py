"""API schemas for sources, sessions and live processing events."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SessionState = Literal["IDLE", "STARTING", "RUNNING", "STOPPING", "STOPPED", "COMPLETED", "ERROR"]


class SourceOut(BaseModel):
    """A trusted server-side source. No paths, URLs or credentials are ever included."""

    id: str
    label: str
    type: Literal["file", "rtsp"]
    availability: Literal["available", "unavailable"]
    detail: str | None = None


class WorkerOut(BaseModel):
    connected: bool
    worker_id: str | None = None
    model_available: bool | None = None
    model_detail: str | None = None


class SourcesResponse(BaseModel):
    sources: list[SourceOut]
    worker: WorkerOut


class SessionStats(BaseModel):
    """Detector/ingestion counters. ``detections_total`` sums faces over processed frames and is
    NOT a number of distinct people."""

    model_config = ConfigDict(extra="ignore")

    frames_read: int = 0
    frames_processed: int = 0
    frames_skipped: int = 0
    frames_dropped: int = 0
    corrupt_frames: int = 0
    detections_total: int = 0
    faces_last_frame: int = 0
    max_faces_in_frame: int = 0
    last_inference_ms: float = 0
    avg_inference_ms: float = 0
    processing_fps: float = 0
    last_frame_id: int = -1
    frame_width: int = 0
    frame_height: int = 0
    source_fps: float = 0
    elapsed_seconds: float = 0


class SessionError(BaseModel):
    code: str
    message: str


class SessionSettings(BaseModel):
    model: str
    frame_skip: int
    confidence_threshold: float


class SessionOut(BaseModel):
    id: str
    state: SessionState
    source: SourceOut | None
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    stop_reason: str | None
    error: SessionError | None
    # Live transport state of the source ("connected" | "reconnecting"), when known.
    connection: str | None = None
    stats: SessionStats
    settings: SessionSettings | None = None


class StartSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")


class ActiveSessionResponse(BaseModel):
    session: SessionOut | None


class SessionListResponse(BaseModel):
    sessions: list[SessionOut]
