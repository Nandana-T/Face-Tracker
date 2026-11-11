"""Entry/exit events and visits (authenticated; evidence images are biometric - private, no static serving)."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import ColumnElement, Select, func, or_, select

from app.core.errors import AppError
from app.models.visit import Visit, VisitEvent
from app.schemas.events import (
    EventListResponse,
    EventOut,
    EventsHealth,
    LogVerification,
    ReconcileResult,
    VisitDetail,
    VisitListResponse,
    VisitorRef,
    VisitOut,
)
from app.schemas.system import ErrorResponse
from app.services.event_audit import verify_events_log
from app.services.event_recorder import aware
from app.services.identity_manager import visitor_label
from app.services.image_store import ImageStoreError
from app.services.visit_service import VisitService

router = APIRouter()
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse}}
_ID = re.compile(r"^[0-9a-f]{32}$")
_LABEL = re.compile(r"^[Vv]?(\d{1,9})$")
LIVE = ("ACTIVE", "TEMPORARILY_MISSING", "EXIT_PENDING")


def _service(request: Request) -> VisitService:
    service: VisitService | None = request.app.state.visits
    if service is None:
        raise AppError(503, "events_disabled", "The visit event log is switched off.")
    return service


def _visitor(vid: str | None, number: int | None) -> VisitorRef | None:
    if vid is None:
        return None
    return VisitorRef(id=vid, number=number, label=visitor_label(number or 0))


def _event_out(ev: VisitEvent, visit: Visit) -> EventOut:
    terminal = ev.slot == "terminal"
    return EventOut(
        id=ev.id,
        visit_id=ev.visit_id,
        type=ev.event_type,
        visitor=_visitor(ev.visitor_id, ev.visitor_number),
        session_id=visit.session_id,
        source_id=visit.source_id,
        source_label=visit.source_label,
        event_time=aware(ev.event_time),
        video_ms=ev.video_ms,
        recorded_at=aware(ev.recorded_at),
        image_status=ev.image_status,
        has_image=ev.image_status == "STORED",
        image_kind=ev.image_kind,
        persistence_status=ev.persistence_status,
        exit_basis=visit.exit_basis if terminal else None,
        closure_reason=visit.closure_reason if terminal else None,
    )


def _integrity(visit: Visit, events: list[VisitEvent]) -> str:
    if any(e.persistence_status == "DEAD" for e in events):
        return "delivery_failed"
    if any(e.image_status in ("MISSING", "UNAVAILABLE") for e in events):
        return "evidence_missing"
    if any(e.persistence_status == "PENDING" or e.image_status == "PENDING" for e in events):
        return "pending"
    return "open" if visit.state in LIVE else "complete"


def _visit_out(visit: Visit, events: list[VisitEvent]) -> VisitOut:
    duration = None
    if visit.state == "CLOSED" and visit.exit_video_ms is not None:
        duration = round(max(0, visit.exit_video_ms - visit.entry_video_ms) / 1000.0, 1)
    return VisitOut(
        id=visit.id,
        visitor=_visitor(visit.visitor_id, visit.visitor_number),
        session_id=visit.session_id,
        source_id=visit.source_id,
        source_label=visit.source_label,
        state=visit.state,
        entry_time=aware(visit.entry_time),
        last_observed_time=aware(visit.last_observed_time),
        exit_time=aware(visit.exit_time) if visit.exit_time else None,
        exit_confirmed_at=aware(visit.exit_confirmed_at) if visit.exit_confirmed_at else None,
        exit_basis=visit.exit_basis,
        closure_reason=visit.closure_reason,
        track_segments=visit.track_segments,
        duration_seconds=duration,
        integrity=_integrity(visit, events),
    )


def _visitor_condition(term: str, column_number: Any, column_id: Any) -> ColumnElement[bool] | None:
    conditions: list[ColumnElement[bool]] = []
    m = _LABEL.match(term)
    if m:
        conditions.append(column_number == int(m.group(1)))
    hexish = re.sub(r"[^0-9a-fA-F]", "", term).lower()
    if len(term) >= 4 and hexish == term.lower():
        conditions.append(column_id.like(f"{hexish}%"))
    if not conditions:
        return None
    return or_(*conditions) if len(conditions) > 1 else conditions[0]


# ====================================================================== static routes first
@router.get(
    "/events/health", response_model=EventsHealth, summary="Persistence health of the event log"
)
async def events_health(request: Request) -> EventsHealth:
    service = request.app.state.visits
    if service is None:
        return EventsHealth(
            enabled=False, events_by_persistence={}, events_by_image_status={}, open_visits=0,
            pending_image_retries=0, counters={}, latency={}, last_reconcile=None, runtime={},
            events_log_present=False, events_log_bytes=0,
        )  # fmt: skip
    log_path = request.app.state.config.events_log_path

    def run() -> EventsHealth:
        health = service.recorder.health()
        return EventsHealth(
            enabled=True,
            events_by_persistence=health["events_by_persistence"],
            events_by_image_status=health["events_by_image_status"],
            open_visits=health["open_visits"],
            pending_image_retries=health["pending_image_retries"],
            counters=health["counters"],
            latency=health["latency"],
            last_reconcile=health["last_reconcile"],
            runtime=service.runtime_stats(),
            events_log_present=log_path.is_file(),
            events_log_bytes=log_path.stat().st_size if log_path.is_file() else 0,
        )

    return await asyncio.to_thread(run)


@router.post(
    "/events/reconcile", response_model=ReconcileResult, summary="Run event reconciliation now"
)
async def reconcile_events(request: Request) -> ReconcileResult:
    service = _service(request)
    live = request.app.state.sessions.live
    live_ids = {live.id} if live is not None else set()
    return ReconcileResult(result=await asyncio.to_thread(service.recorder.reconcile, live_ids))


@router.get(
    "/events/log-verification",
    response_model=LogVerification,
    summary="Compare events.log with the database",
)
async def log_verification(request: Request) -> LogVerification:
    service = _service(request)
    path = request.app.state.config.events_log_path
    report = await asyncio.to_thread(verify_events_log, service.recorder._factory, path)  # noqa: SLF001
    return LogVerification(**report)


# ====================================================================== events
@router.get("/events", response_model=EventListResponse, summary="Entry / exit events (paged)")
async def list_events(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    type: Annotated[Literal["entry", "exit", "interrupted"] | None, Query()] = None,  # noqa: A002
    visitor: Annotated[str | None, Query(max_length=40)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    session_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    visit_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    persistence_status: Annotated[Literal["PENDING", "DELIVERED", "DEAD"] | None, Query()] = None,
    sort: Literal["event_time", "recorded_at"] = "event_time",
    order: Literal["asc", "desc"] = "desc",
) -> EventListResponse:
    service = _service(request)

    def run() -> EventListResponse:
        stmt: Select[Any] = select(VisitEvent, Visit).join(Visit, Visit.id == VisitEvent.visit_id)
        count: Select[Any] = (
            select(func.count())
            .select_from(VisitEvent)
            .join(Visit, Visit.id == VisitEvent.visit_id)
        )
        conds: list[ColumnElement[bool]] = []
        if type:
            conds.append(VisitEvent.event_type == type)
        if since:
            conds.append(VisitEvent.event_time >= aware(since))
        if until:
            conds.append(VisitEvent.event_time <= aware(until))
        if session_id:
            conds.append(Visit.session_id == session_id)
        if visit_id:
            conds.append(VisitEvent.visit_id == visit_id)
        if persistence_status:
            conds.append(VisitEvent.persistence_status == persistence_status)
        if visitor and visitor.strip():
            cond = _visitor_condition(
                visitor.strip(), VisitEvent.visitor_number, VisitEvent.visitor_id
            )
            if cond is None:
                return EventListResponse(items=[], total=0, limit=limit, offset=offset)
            conds.append(cond)
        for c in conds:
            stmt, count = stmt.where(c), count.where(c)
        column = VisitEvent.event_time if sort == "event_time" else VisitEvent.recorded_at
        # Ties (an interrupted visit that was never observed again has exit time == entry time): the
        # entry precedes its terminal event in time order ('entry' < 'terminal'), so reverse for newest-first.
        desc = order == "desc"
        stmt = stmt.order_by(
            column.desc() if desc else column.asc(),
            VisitEvent.slot.desc() if desc else VisitEvent.slot.asc(),
            VisitEvent.id,
        )
        with service.recorder._factory() as db:  # noqa: SLF001
            total = int(db.execute(count).scalar_one())
            rows = db.execute(stmt.limit(limit).offset(offset)).all()
            return EventListResponse(
                items=[_event_out(ev, visit) for ev, visit in rows],
                total=total,
                limit=limit,
                offset=offset,
            )

    return await asyncio.to_thread(run)


def _load_event(service: VisitService, event_id: str) -> tuple[VisitEvent, Visit]:
    if not _ID.match(event_id):
        raise AppError(404, "event_not_found", "No such event.")
    with service.recorder._factory() as db:  # noqa: SLF001
        ev = db.get(VisitEvent, event_id)
        visit = db.get(Visit, ev.visit_id) if ev else None
        if ev is None or visit is None:
            raise AppError(404, "event_not_found", "No such event.")
        db.expunge_all()
        return ev, visit


@router.get(
    "/events/{event_id}", response_model=EventOut, responses=_NOT_FOUND, summary="One event"
)
async def get_event(event_id: str, request: Request) -> EventOut:
    service = _service(request)

    def run() -> EventOut:
        ev, visit = _load_event(service, event_id)
        return _event_out(ev, visit)

    return await asyncio.to_thread(run)


@router.get(
    "/events/{event_id}/image",
    responses={**_NOT_FOUND, 200: {"content": {"image/jpeg": {}}}},
    summary="Evidence face crop (authenticated; never cached or served statically)",
)
async def event_image(event_id: str, request: Request) -> Response:
    service = _service(request)

    def run() -> bytes:
        ev, _ = _load_event(service, event_id)
        if ev.image_status != "STORED" or not ev.image_path:
            raise AppError(404, "image_not_found", "This event has no stored image.")
        try:
            return service.recorder.store.read(ev.image_path)
        except ImageStoreError as exc:
            raise AppError(404, "image_not_found", "The stored image is unavailable.") from exc

    data = await asyncio.to_thread(run)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


# ====================================================================== visits
@router.get("/visits", response_model=VisitListResponse, summary="Visits (paged)")
async def list_visits(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    state: Annotated[
        Literal["ACTIVE", "TEMPORARILY_MISSING", "EXIT_PENDING", "CLOSED", "INTERRUPTED"] | None,
        Query(),
    ] = None,
    visitor: Annotated[str | None, Query(max_length=40)] = None,
    since: datetime | None = None,
    until: datetime | None = None,
    sort: Literal["entry_time", "exit_time"] = "entry_time",
    order: Literal["asc", "desc"] = "desc",
) -> VisitListResponse:
    service = _service(request)

    def run() -> VisitListResponse:
        stmt: Select[Any] = select(Visit)
        count: Select[Any] = select(func.count()).select_from(Visit)
        conds: list[ColumnElement[bool]] = []
        if state:
            conds.append(Visit.state == state)
        if since:
            conds.append(Visit.entry_time >= aware(since))
        if until:
            conds.append(Visit.entry_time <= aware(until))
        if visitor and visitor.strip():
            cond = _visitor_condition(visitor.strip(), Visit.visitor_number, Visit.visitor_id)
            if cond is None:
                return VisitListResponse(items=[], total=0, limit=limit, offset=offset)
            conds.append(cond)
        for c in conds:
            stmt, count = stmt.where(c), count.where(c)
        column = Visit.entry_time if sort == "entry_time" else Visit.exit_time
        stmt = stmt.order_by(column.desc() if order == "desc" else column.asc(), Visit.id)
        with service.recorder._factory() as db:  # noqa: SLF001
            total = int(db.execute(count).scalar_one())
            visits = db.execute(stmt.limit(limit).offset(offset)).scalars().all()
            by_visit: dict[str, list[VisitEvent]] = {v.id: [] for v in visits}
            if visits:
                for ev in db.execute(
                    select(VisitEvent).where(VisitEvent.visit_id.in_(list(by_visit)))
                ).scalars():
                    by_visit[ev.visit_id].append(ev)
            return VisitListResponse(
                items=[_visit_out(v, by_visit[v.id]) for v in visits],
                total=total,
                limit=limit,
                offset=offset,
            )

    return await asyncio.to_thread(run)


@router.get(
    "/visits/{visit_id}",
    response_model=VisitDetail,
    responses=_NOT_FOUND,
    summary="One visit with its events",
)
async def get_visit(visit_id: str, request: Request) -> VisitDetail:
    service = _service(request)

    def run() -> VisitDetail:
        if not _ID.match(visit_id):
            raise AppError(404, "visit_not_found", "No such visit.")
        with service.recorder._factory() as db:  # noqa: SLF001
            visit = db.get(Visit, visit_id)
            if visit is None:
                raise AppError(404, "visit_not_found", "No such visit.")
            events = list(
                db.execute(
                    select(VisitEvent)
                    .where(VisitEvent.visit_id == visit_id)
                    .order_by(VisitEvent.event_time, VisitEvent.slot.asc())
                ).scalars()
            )
            base = _visit_out(visit, events)
            return VisitDetail(**base.model_dump(), events=[_event_out(e, visit) for e in events])

    return await asyncio.to_thread(run)


@router.get(
    "/visits/{visit_id}/events",
    response_model=list[EventOut],
    responses=_NOT_FOUND,
    summary="Events of one visit",
)
async def visit_events(visit_id: str, request: Request) -> list[EventOut]:
    return (await get_visit(visit_id, request)).events
