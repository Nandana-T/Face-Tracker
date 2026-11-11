"""System status / readiness / sanitised config use-cases."""

from __future__ import annotations

import time

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.core.config import AppConfig
from app.core.settings import Settings
from app.db.session import DatabaseCheck, check_database
from app.repositories.migrations import current_revision
from app.schemas.system import (
    ComponentStatus,
    DatabaseStatus,
    DependencyCheck,
    PublicConfigResponse,
    PublicDetectionConfig,
    PublicLoggingConfig,
    PublicStreamConfig,
    PublicVideoConfig,
    ReadinessChecks,
    ReadinessResponse,
    SystemComponents,
    SystemStatusResponse,
)
from app.services.session_manager import SessionManager

SERVICE_NAME = "katomaran-api"


def database_check_model(check: DatabaseCheck) -> DependencyCheck:
    return DependencyCheck(
        status="ok" if check.ok else "unavailable", latency_ms=check.latency_ms, error=check.error
    )


def readiness(engine: Engine, settings: Settings) -> ReadinessResponse:
    check = check_database(engine, settings.db_connect_retries, settings.db_retry_backoff_seconds)
    return ReadinessResponse(
        status="ready" if check.ok else "not_ready",
        checks=ReadinessChecks(database=database_check_model(check)),
    )


def system_status(
    session: Session,
    settings: Settings,
    config: AppConfig,
    started_at: float,
    sessions: SessionManager,
) -> SystemStatusResponse:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    check = check_database(engine, settings.db_connect_retries, settings.db_retry_backoff_seconds)
    revision = current_revision(session) if check.ok else None
    worker = sessions.worker
    if worker is None:
        worker_c = ComponentStatus(
            state="not_running", detail="No worker connected. Start scripts\start-worker.ps1."
        )
        det_c = ComponentStatus(
            state="not_running", detail="Model status is reported by the worker once connected."
        )
        track_c = ComponentStatus(
            state="not_running", detail="Tracking runs in the worker once it connects."
        )
        recog_c = ComponentStatus(
            state="not_running", detail="Recognition runs in the worker once it connects."
        )
    else:
        worker_c = ComponentStatus(
            state="active" if sessions.live else "ready",
            detail=f"Worker '{worker.worker_id}' connected.",
        )
        det_c = (
            ComponentStatus(state="ready", detail=f"Model '{config.detection.model}' available.")
            if worker.model_available
            else ComponentStatus(
                state="unavailable", detail=worker.model_detail or "Model unavailable."
            )
        )
        track_c = (
            ComponentStatus(state="ready", detail="Face tracking runs in the worker.")
            if config.tracking.enabled
            else ComponentStatus(
                state="unavailable", detail="Tracking is switched off in the configuration."
            )
        )
        if not config.recognition.enabled:
            recog_c = ComponentStatus(
                state="unavailable", detail="Recognition is switched off in the configuration."
            )
        elif worker.recognition_available:
            recog_c = ComponentStatus(
                state="ready", detail=f"Model '{config.recognition.model}' available."
            )
        else:
            recog_c = ComponentStatus(
                state="unavailable",
                detail=worker.recognition_detail or "The recognition model is unavailable.",
            )
    return SystemStatusResponse(
        service=SERVICE_NAME,
        version=settings.app_version,
        environment=settings.app_env,
        uptime_seconds=round(time.monotonic() - started_at, 1),
        database=DatabaseStatus(
            status="ok" if check.ok else "unavailable",
            migration_revision=revision,
            migrations_applied=revision is not None,
        ),
        components=SystemComponents(
            video_worker=worker_c,
            face_detector=det_c,
            face_recognizer=recog_c,
            tracker=track_c,
        ),
        active_video_sources=1 if sessions.live else 0,
        video_source_configured=bool(config.video.source.strip() or config.video.sources),
    )


def public_config(settings: Settings, config: AppConfig) -> PublicConfigResponse:
    return PublicConfigResponse(
        environment=settings.app_env,
        video=PublicVideoConfig(
            source_type=config.video.source_type,
            source_configured=bool(config.video.source.strip() or config.video.sources),
            reconnect_interval_seconds=config.video.reconnect_interval_seconds,
        ),
        detection=PublicDetectionConfig(
            frame_skip=config.detection.frame_skip,
            confidence_threshold=config.detection.confidence_threshold,
            model=config.detection.model,
            input_size=config.detection.input_size,
        ),
        stream=PublicStreamConfig(max_fps=config.stream.max_fps, max_width=config.stream.max_width),
        recognition=config.recognition.model_dump(exclude={"model_path", "faces_dir"}),
        tracking=config.tracking,
        logging=PublicLoggingConfig(level=settings.log_level or config.logging.level),
    )
