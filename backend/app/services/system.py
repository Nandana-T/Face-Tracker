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
    PublicLoggingConfig,
    PublicVideoConfig,
    ReadinessChecks,
    ReadinessResponse,
    SystemComponents,
    SystemStatusResponse,
)

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
    session: Session, settings: Settings, config: AppConfig, started_at: float
) -> SystemStatusResponse:
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    check = check_database(engine, settings.db_connect_retries, settings.db_retry_backoff_seconds)
    revision = current_revision(session) if check.ok else None
    phase = "Planned for Phase {n}."
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
            video_worker=ComponentStatus(state="not_implemented", detail=phase.format(n=1)),
            face_detector=ComponentStatus(state="not_implemented", detail=phase.format(n=1)),
            face_recognizer=ComponentStatus(state="not_implemented", detail=phase.format(n=2)),
            tracker=ComponentStatus(state="not_implemented", detail=phase.format(n=3)),
        ),
        active_video_sources=0,
        video_source_configured=bool(config.video.source.strip()),
    )


def public_config(settings: Settings, config: AppConfig) -> PublicConfigResponse:
    return PublicConfigResponse(
        environment=settings.app_env,
        video=PublicVideoConfig(
            source_type=config.video.source_type,
            source_configured=bool(config.video.source.strip()),
            reconnect_interval_seconds=config.video.reconnect_interval_seconds,
        ),
        detection=config.detection,
        recognition=config.recognition,
        tracking=config.tracking,
        logging=PublicLoggingConfig(level=settings.log_level or config.logging.level),
    )
