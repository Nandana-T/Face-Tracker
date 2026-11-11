"""Analytics API (authenticated). Definitions: app/services/analytics.py and docs/phase-5-report.md.

Every number is a database aggregate; the live stream only says "something changed" so clients
refetch - it never carries counts that could drift from PostgreSQL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Request, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.api.v1.events import _event_out
from app.api.v1.processing import StreamTicket
from app.core.errors import AppError
from app.schemas.analytics import (
    CoverageOut,
    OverviewOut,
    ReconciliationOut,
    SessionsOut,
    SourcesOut,
    TimelineOut,
)
from app.schemas.events import EventListResponse
from app.services.analytics import AnalyticsService, Scope
from app.services.session_manager import SessionManager
from app.workers import protocol as P

logger = logging.getLogger("katomaran.analytics")
router = APIRouter()
ANALYTICS_STREAM = "analytics"  # the "session id" a stream ticket for this socket is bound to


def _service(request: Request) -> AnalyticsService:
    service: AnalyticsService | None = request.app.state.analytics
    if service is None:
        raise AppError(503, "analytics_disabled", "Analytics are unavailable.")
    return service


def _scope(
    since: datetime | None,
    until: datetime | None,
    source_id: str | None,
    session_id: str | None,
) -> Scope:
    if since and until and since >= until:
        raise AppError(422, "invalid_range", "`since` must be before `until`.")
    return Scope(since, until, source_id, session_id)


Since = Annotated[datetime | None, Query(description="Inclusive start (ISO 8601; naive = UTC)")]
Until = Annotated[datetime | None, Query(description="Exclusive end (ISO 8601; naive = UTC)")]
Source = Annotated[str | None, Query(max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")]
SessionId = Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")]


@router.get("/analytics/overview", response_model=OverviewOut, summary="Counts for a scope")
async def overview(
    request: Request,
    since: Since = None,
    until: Until = None,
    source_id: Source = None,
    session_id: SessionId = None,
) -> OverviewOut:
    scope = _scope(since, until, source_id, session_id)
    return OverviewOut(**await asyncio.to_thread(_service(request).overview, scope))


@router.get(
    "/analytics/timeline",
    response_model=TimelineOut,
    summary="Visits / entries / exits over time (UTC buckets)",
)
async def timeline(
    request: Request,
    since: Since = None,
    until: Until = None,
    source_id: Source = None,
    session_id: SessionId = None,
    bucket: Literal["hour", "day"] = "day",
) -> TimelineOut:
    scope = _scope(since, until, source_id, session_id)
    return TimelineOut(**await asyncio.to_thread(_service(request).timeline, scope, bucket))


@router.get("/analytics/sources", response_model=SourcesOut, summary="Per-source breakdown")
async def sources(
    request: Request,
    since: Since = None,
    until: Until = None,
    source_id: Source = None,
    session_id: SessionId = None,
) -> SourcesOut:
    scope = _scope(since, until, source_id, session_id)
    return SourcesOut(**await asyncio.to_thread(_service(request).sources, scope))


@router.get(
    "/analytics/sessions",
    response_model=SessionsOut,
    summary="Per-session breakdown (paged, newest first)",
)
async def sessions(
    request: Request,
    since: Since = None,
    until: Until = None,
    source_id: Source = None,
    session_id: SessionId = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> SessionsOut:
    scope = _scope(since, until, source_id, session_id)
    return SessionsOut(**await asyncio.to_thread(_service(request).sessions, scope, limit, offset))


@router.get(
    "/analytics/coverage",
    response_model=CoverageOut,
    summary="Recognition coverage diagnostics (NOT person-level recall)",
)
async def coverage(
    request: Request,
    since: Since = None,
    until: Until = None,
    source_id: Source = None,
    session_id: SessionId = None,
) -> CoverageOut:
    scope = _scope(since, until, source_id, session_id)
    return CoverageOut(**await asyncio.to_thread(_service(request).coverage, scope))


@router.get(
    "/analytics/recent",
    response_model=EventListResponse,
    summary="Most recent entry/exit events in scope",
)
async def recent(
    request: Request,
    since: Since = None,
    until: Until = None,
    source_id: Source = None,
    session_id: SessionId = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> EventListResponse:
    scope = _scope(since, until, source_id, session_id)
    rows = await asyncio.to_thread(_service(request).recent, scope, limit)
    items = [_event_out(ev, visit) for ev, visit in rows]
    return EventListResponse(items=items, total=len(items), limit=limit, offset=0)


@router.get(
    "/analytics/reconciliation",
    response_model=ReconciliationOut,
    summary="Recompute the global counts independently and report differences (read-only)",
)
async def reconciliation(request: Request) -> ReconciliationOut:
    return ReconciliationOut(**await asyncio.to_thread(_service(request).reconcile))


@router.post(
    "/analytics/stream-ticket",
    response_model=StreamTicket,
    summary="Single-use ticket for the analytics change stream",
)
async def analytics_ticket(request: Request) -> StreamTicket:
    ticket, ttl = request.app.state.auth.issue_ticket(ANALYTICS_STREAM)
    return StreamTicket(ticket=ticket, expires_in=ttl)


ws_router = APIRouter()


@ws_router.websocket("/analytics/stream")
async def analytics_stream(websocket: WebSocket) -> None:
    """Change notifications only (`{"type":"analytics_changed","reason":...}`), never counts."""
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in websocket.app.state.settings.cors_origins:
        await websocket.close(code=1008)
        return
    if not websocket.app.state.auth.consume_ticket(
        websocket.query_params.get("ticket"), ANALYTICS_STREAM
    ):
        await websocket.close(code=1008)
        return
    mgr: SessionManager = websocket.app.state.sessions
    sub = mgr.subscribe_analytics()
    await websocket.accept()
    try:
        await websocket.send_text(
            P.encode_control(
                {"type": "hello", "at": datetime.now(UTC).isoformat(timespec="seconds")}
            )
        )

        async def writer() -> None:
            while True:
                controls, _frame = await sub.next_batch()
                for text in controls:
                    await websocket.send_text(text)

        async def reader() -> None:
            while True:
                if (await websocket.receive())["type"] == "websocket.disconnect":
                    return

        tasks = [asyncio.create_task(writer()), asyncio.create_task(reader())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    finally:
        mgr.unsubscribe_analytics(sub)
        if websocket.application_state == WebSocketState.CONNECTED:
            with contextlib.suppress(Exception):
                await websocket.close(code=1000)
