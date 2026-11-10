"""Sources and processing-session lifecycle (REST)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, Response

from app.core.errors import AppError
from app.schemas.processing import (
    ActiveSessionResponse,
    SessionListResponse,
    SessionOut,
    SessionStats,
    SourceOut,
    SourcesResponse,
    StartSessionRequest,
)
from app.schemas.system import ErrorResponse
from app.services.session_manager import SessionManager

router = APIRouter()

_ERRORS: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    503: {"model": ErrorResponse},
}


def manager(request: Request) -> SessionManager:
    mgr: SessionManager = request.app.state.sessions
    return mgr


@router.get(
    "/sources", response_model=SourcesResponse, summary="Supported sources and worker state"
)
async def list_sources(request: Request) -> SourcesResponse:
    return manager(request).sources_response()


@router.get(
    "/sources/{source_id}",
    response_model=SourceOut,
    responses={404: {"model": ErrorResponse}},
    summary="Source information (no paths or URLs)",
)
async def get_source(source_id: str, request: Request) -> SourceOut:
    found = manager(request).source_out(source_id)
    if found is None:
        raise AppError(404, "source_not_found", "Unknown video source.")
    return found


@router.post(
    "/sessions",
    response_model=SessionOut,
    status_code=202,
    responses=_ERRORS,
    summary="Start a processing session (idempotent for the same source)",
)
async def start_session(
    body: StartSessionRequest, request: Request, response: Response
) -> SessionOut:
    session, created = await manager(request).start(body.source_id)
    response.status_code = 202 if created else 200
    return session


@router.get(
    "/sessions/active", response_model=ActiveSessionResponse, summary="Currently active session"
)
async def active_session(request: Request) -> ActiveSessionResponse:
    return ActiveSessionResponse(session=await manager(request).active_session())


@router.get("/sessions", response_model=SessionListResponse, summary="Recent sessions")
async def recent_sessions(
    request: Request, limit: Annotated[int, Query(ge=1, le=50)] = 10
) -> SessionListResponse:
    return SessionListResponse(sessions=await manager(request).recent_sessions(limit))


@router.get(
    "/sessions/{session_id}",
    response_model=SessionOut,
    responses={404: {"model": ErrorResponse}},
    summary="Processing status of a session",
)
async def get_session(session_id: str, request: Request) -> SessionOut:
    return await manager(request).get_session(session_id)


@router.get(
    "/sessions/{session_id}/stats",
    response_model=SessionStats,
    responses={404: {"model": ErrorResponse}},
    summary="Detection statistics (not visitor counts)",
)
async def session_stats(session_id: str, request: Request) -> SessionStats:
    return (await manager(request).get_session(session_id)).stats


@router.post(
    "/sessions/{session_id}/stop",
    response_model=SessionOut,
    responses={404: {"model": ErrorResponse}},
    summary="Stop a session (idempotent)",
)
async def stop_session(session_id: str, request: Request) -> SessionOut:
    return await manager(request).stop(session_id)
