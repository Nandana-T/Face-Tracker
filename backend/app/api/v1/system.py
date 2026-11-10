"""Health, readiness, system status and configuration endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.deps import ConfigDep, EngineDep, SessionDep, SettingsDep
from app.core.security import require_auth
from app.schemas.system import (
    HealthResponse,
    PublicConfigResponse,
    ReadinessResponse,
    SystemStatusResponse,
)
from app.services import system as system_service

router = APIRouter()


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
def health() -> HealthResponse:
    """Process liveness only. Never touches dependencies."""
    return HealthResponse(status="ok", service=system_service.SERVICE_NAME)


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse, "description": "A required dependency is down"}},
    summary="Readiness probe (verifies database connectivity)",
)
def ready(engine: EngineDep, settings: SettingsDep) -> JSONResponse:
    result = system_service.readiness(engine, settings)
    return JSONResponse(
        status_code=200 if result.status == "ready" else 503, content=result.model_dump()
    )


@router.get(
    "/system/status",
    response_model=SystemStatusResponse,
    summary="Structured system status",
    dependencies=[Depends(require_auth)],
)
def system_status(
    request: Request, session: SessionDep, settings: SettingsDep, config: ConfigDep
) -> SystemStatusResponse:
    return system_service.system_status(
        session, settings, config, request.app.state.started_at, request.app.state.sessions
    )


@router.get(
    "/config",
    response_model=PublicConfigResponse,
    summary="Sanitised configuration",
    dependencies=[Depends(require_auth)],
)
def get_config(settings: SettingsDep, config: ConfigDep) -> PublicConfigResponse:
    return system_service.public_config(settings, config)
