"""API response schemas for health, readiness, system status and configuration."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from app.core.config import TrackingConfig


class HealthResponse(BaseModel):
    status: Literal["ok"]
    service: str


class DependencyCheck(BaseModel):
    status: Literal["ok", "unavailable"]
    latency_ms: float | None = None
    error: str | None = None


class ReadinessChecks(BaseModel):
    database: DependencyCheck


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks


ComponentState = Literal[
    "not_implemented", "not_configured", "not_running", "ready", "unavailable", "active"
]


class ComponentStatus(BaseModel):
    state: ComponentState
    detail: str


class DatabaseStatus(BaseModel):
    status: Literal["ok", "unavailable"]
    migration_revision: str | None
    migrations_applied: bool


class SystemComponents(BaseModel):
    video_worker: ComponentStatus
    face_detector: ComponentStatus
    face_recognizer: ComponentStatus
    tracker: ComponentStatus


class SystemStatusResponse(BaseModel):
    service: str
    version: str
    environment: str
    uptime_seconds: float
    database: DatabaseStatus
    components: SystemComponents
    active_video_sources: int
    video_source_configured: bool


class PublicVideoConfig(BaseModel):
    source_type: Literal["file", "rtsp"]
    source_configured: bool
    reconnect_interval_seconds: float


class PublicDetectionConfig(BaseModel):
    frame_skip: int
    confidence_threshold: float
    model: str
    input_size: int


class PublicStreamConfig(BaseModel):
    max_fps: float
    max_width: int


class PublicLoggingConfig(BaseModel):
    level: str


class PublicConfigResponse(BaseModel):
    """Sanitised configuration: no credentials, URLs, connection strings or absolute paths."""

    environment: str
    video: PublicVideoConfig
    detection: PublicDetectionConfig
    stream: PublicStreamConfig
    # RecognitionConfig without filesystem paths (model_path, faces_dir) - see services/system.py
    recognition: dict[str, Any]
    tracking: TrackingConfig
    logging: PublicLoggingConfig


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: object | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
