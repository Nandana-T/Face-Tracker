"""Visitor gallery + recognition status (authenticated; biometric data - no embeddings, ever)."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import ColumnElement, Select, func, or_, select

from app.core.errors import AppError
from app.models.processing_session import ProcessingSession
from app.models.visitor import IdentityAssociation, Visitor
from app.schemas.system import ErrorResponse
from app.schemas.visitors import (
    AssociationOut,
    RecognitionStats,
    RecognitionStatus,
    VisitorDetail,
    VisitorListResponse,
    VisitorOut,
)
from app.services.identity_manager import IdentityManager, visitor_label
from app.services.image_store import ImageStoreError
from app.services.session_manager import SessionManager

router = APIRouter()
_NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorResponse}}
_ID = re.compile(r"^[0-9a-f]{32}$")
_LABEL = re.compile(r"^[Vv]?(\d{1,9})$")


def _identity(request: Request) -> IdentityManager:
    identity: IdentityManager | None = request.app.state.identity
    if identity is None:
        raise AppError(503, "recognition_disabled", "Face recognition is switched off.")
    return identity


def _to_out(v: Visitor) -> VisitorOut:
    return VisitorOut(
        id=v.id,
        number=v.visitor_number,
        label=visitor_label(v.visitor_number),
        status=v.status,
        first_seen_at=v.first_seen_at,
        last_seen_at=v.last_seen_at,
        recognition_count=v.recognition_count,
        has_image=bool(v.image_path),
        model_version=v.model_version,
    )


def _search_condition(term: str) -> ColumnElement[bool] | None:
    """Match by short label/number (V00012, 12) and/or the start of the persistent id."""
    conditions: list[ColumnElement[bool]] = []
    m = _LABEL.match(term)
    if m:
        conditions.append(Visitor.visitor_number == int(m.group(1)))
    hexish = re.sub(r"[^0-9a-fA-F]", "", term).lower()
    if len(term) >= 4 and hexish == term.lower():
        conditions.append(Visitor.id.like(f"{hexish}%"))
    if not conditions:
        return None
    return or_(*conditions) if len(conditions) > 1 else conditions[0]


def _find(db_visitor_id: str) -> Select[tuple[Visitor]]:
    """Accept the persistent id (32 hex) or the short label (V00124 / 124)."""
    if _ID.match(db_visitor_id):
        return select(Visitor).where(Visitor.id == db_visitor_id)
    m = _LABEL.match(db_visitor_id)
    if m:
        return select(Visitor).where(Visitor.visitor_number == int(m.group(1)))
    raise AppError(404, "visitor_not_found", "No such visitor.")


@router.get("/visitors", response_model=VisitorListResponse, summary="Registered visitors (paged)")
async def list_visitors(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 24,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    q: Annotated[str | None, Query(max_length=40)] = None,
    sort: Literal["last_seen", "first_seen", "number"] = "last_seen",
    order: Literal["asc", "desc"] = "desc",
) -> VisitorListResponse:
    identity = _identity(request)

    def run() -> VisitorListResponse:
        stmt = select(Visitor)
        count = select(func.count()).select_from(Visitor)
        if q and q.strip():
            cond = _search_condition(q.strip())
            if cond is None:
                return VisitorListResponse(items=[], total=0, limit=limit, offset=offset)
            stmt, count = stmt.where(cond), count.where(cond)
        column = {
            "last_seen": Visitor.last_seen_at,
            "first_seen": Visitor.first_seen_at,
            "number": Visitor.visitor_number,
        }[sort]
        stmt = stmt.order_by(
            column.desc() if order == "desc" else column.asc(), Visitor.visitor_number
        )
        with identity._factory() as db:  # noqa: SLF001 - shared session factory
            total = int(db.execute(count).scalar_one())
            rows = db.execute(stmt.limit(limit).offset(offset)).scalars().all()
            return VisitorListResponse(
                items=[_to_out(v) for v in rows], total=total, limit=limit, offset=offset
            )

    return await asyncio.to_thread(run)


@router.get(
    "/visitors/{visitor_id}",
    response_model=VisitorDetail,
    responses=_NOT_FOUND,
    summary="One visitor with its recognition history (no embeddings)",
)
async def get_visitor(visitor_id: str, request: Request) -> VisitorDetail:
    identity = _identity(request)

    def run() -> VisitorDetail:
        with identity._factory() as db:  # noqa: SLF001
            v = db.execute(_find(visitor_id)).scalar_one_or_none()
            if v is None:
                raise AppError(404, "visitor_not_found", "No such visitor.")
            rows = db.execute(
                select(IdentityAssociation, ProcessingSession.source_label)
                .join(ProcessingSession, ProcessingSession.id == IdentityAssociation.session_id)
                .where(IdentityAssociation.visitor_id == v.id)
                .order_by(IdentityAssociation.last_seen_at.desc())
                .limit(50)
            ).all()
            sessions = {a.session_id for a, _ in rows}
            out = _to_out(v)
            return VisitorDetail(
                **out.model_dump(),
                first_session_id=v.first_session_id,
                image_quality=v.image_quality,
                sessions_count=len(sessions),
                tracks_count=len(rows),
                associations=[
                    AssociationOut(
                        session_id=a.session_id,
                        source_label=label,
                        track_id=a.track_id,
                        state=a.state,
                        best_similarity=a.best_similarity,
                        observations=a.observations,
                        conflicts=a.conflicts,
                        registered_in_session=bool(a.registered_here),
                        first_seen_at=a.first_seen_at,
                        last_seen_at=a.last_seen_at,
                    )
                    for a, label in rows
                ],
            )

    return await asyncio.to_thread(run)


@router.get(
    "/visitors/{visitor_id}/image",
    responses={**_NOT_FOUND, 200: {"content": {"image/jpeg": {}}}},
    summary="Representative face image (authenticated; never cached or served statically)",
)
async def visitor_image(visitor_id: str, request: Request) -> Response:
    identity = _identity(request)

    def run() -> bytes:
        with identity._factory() as db:  # noqa: SLF001
            v = db.execute(_find(visitor_id)).scalar_one_or_none()
            if v is None:
                raise AppError(404, "visitor_not_found", "No such visitor.")
            if not v.image_path:
                raise AppError(404, "image_not_found", "This visitor has no stored image.")
            try:
                return identity._store.read(v.image_path)  # noqa: SLF001
            except ImageStoreError as exc:
                raise AppError(404, "image_not_found", "The stored image is unavailable.") from exc

    data = await asyncio.to_thread(run)
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.delete(
    "/visitors/{visitor_id}",
    status_code=204,
    responses=_NOT_FOUND,
    summary="Erase a visitor: embeddings, images and associations (right to erasure)",
)
async def delete_visitor(visitor_id: str, request: Request) -> Response:
    identity = _identity(request)

    def run() -> str | None:
        with identity._factory() as db:  # noqa: SLF001
            v = db.execute(_find(visitor_id)).scalar_one_or_none()
            vid = v.id if v else None
        if not vid:
            return None
        visits = request.app.state.visits
        if visits is not None:
            # Evidence crops of this person's entries/exits are biometric: delete them first, then
            # the identity. (An interrupted erasure is completed by reconciliation.)
            visits.recorder.erase_visitor(vid)
        return vid if identity.delete_visitor(vid) else None

    erased = await asyncio.to_thread(run)
    if erased is None:
        raise AppError(404, "visitor_not_found", "No such visitor.")
    if request.app.state.visits is not None:
        # an open visit of this person ends by absence, recorded de-identified and without evidence
        request.app.state.visits.manager.forget_visitor(erased)
    return Response(status_code=204)


@router.get(
    "/recognition/status", response_model=RecognitionStatus, summary="Recognition availability"
)
async def recognition_status(request: Request) -> RecognitionStatus:
    identity: IdentityManager | None = request.app.state.identity
    mgr: SessionManager = request.app.state.sessions
    worker = mgr.worker
    cfg = request.app.state.config.recognition
    return RecognitionStatus(
        enabled=cfg.enabled,
        model=cfg.model,
        model_version=identity.model_version if identity else None,
        worker_available=worker.recognition_available if worker else None,
        worker_detail=worker.recognition_detail if worker else None,
        registered_visitors=identity.visitor_count() if identity else 0,
        indexed_embeddings=identity.runtime_stats()["indexed_embeddings"] if identity else 0,
    )


@router.get("/recognition/stats", response_model=RecognitionStats, summary="Recognition counters")
async def recognition_stats(request: Request) -> RecognitionStats:
    identity = _identity(request)
    cfg = request.app.state.config.recognition

    def run() -> RecognitionStats:
        with identity._factory() as db:  # noqa: SLF001
            visitors = int(db.execute(select(func.count()).select_from(Visitor)).scalar_one())
            assoc = int(
                db.execute(select(func.count()).select_from(IdentityAssociation)).scalar_one()
            )
        return RecognitionStats(
            registered_visitors=visitors,
            associations=assoc,
            runtime=identity.runtime_stats(),
            thresholds={
                "similarity_threshold": cfg.similarity_threshold,
                "novelty_threshold": cfg.novelty_threshold,
                "ambiguity_margin": cfg.ambiguity_margin,
            },
        )

    return await asyncio.to_thread(run)
