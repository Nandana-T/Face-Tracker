"""Processing-session orchestration on the API side.

Single-API-process design (documented in docs/phase-1-report.md): the manager keeps the live view
of the one active session and the worker connection in memory; PostgreSQL is the durable record
and (through the unique ``active_slot``) the arbiter of "only one active session".

Invariants
* A session is only RUNNING after the worker reported it. The UI may rely on that.
* A non-terminal session always has an accountable worker: if the worker connection drops or its
  heartbeat stops, the session becomes ERROR (``worker_lost``); a session that cannot start
  within ``start_timeout_seconds`` becomes ERROR (``start_timeout``).
* start/stop are idempotent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from app.core.config import AppConfig
from app.core.errors import AppError
from app.core.events import audit
from app.models.processing_session import ProcessingSession
from app.schemas.processing import (
    SessionError,
    SessionOut,
    SessionSettings,
    SessionStats,
    SourceOut,
    SourcesResponse,
    WorkerOut,
)
from app.schemas.tracking import TrackingFrameMeta
from app.services.identity_manager import IdentityManager, parse_observation, visitor_label
from app.services.session_store import (
    ActiveSessionExists,
    InvalidTransition,
    SessionStore,
)
from app.services.visit_manager import TrackSnap
from app.services.visit_service import VisitService
from app.vision.sources.registry import SourceDescriptor, get_descriptor, list_sources
from app.workers import protocol as P

logger = logging.getLogger("katomaran.sessions")

STATS_PERSIST_INTERVAL = 5.0


def log_event(name: str, **fields: Any) -> None:
    """Session lifecycle record in the structured app log (events.log is reserved for visitor
    entry/exit events, which do not exist yet)."""
    logger.info(name, extra=fields)


ACTIVE = ("STARTING", "RUNNING", "STOPPING")


class WorkerSocket(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


@dataclass
class WorkerLink:
    ws: WorkerSocket
    worker_id: str = "unknown"
    model_available: bool | None = None
    model_detail: str | None = None
    recognition_available: bool | None = None
    recognition_detail: str | None = None
    last_seen: float = field(default_factory=time.monotonic)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.ws.send_text(P.encode_control(message))


class Subscriber:
    """One browser stream: ordered control messages + a latest-wins frame slot (bounded)."""

    def __init__(self) -> None:
        self.control: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self.frame: bytes | None = None
        self.wake = asyncio.Event()
        self.dropped_frames = 0
        self.closed = False

    def push_control(self, text: str) -> None:
        try:
            self.control.put_nowait(text)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self.control.get_nowait()  # drop oldest; control traffic is low-rate
            self.control.put_nowait(text)
        self.wake.set()

    def push_frame(self, data: bytes) -> None:
        if self.frame is not None:
            self.dropped_frames += 1
        self.frame = data
        self.wake.set()

    async def next_batch(self) -> tuple[list[str], bytes | None]:
        await self.wake.wait()
        self.wake.clear()
        controls: list[str] = []
        while not self.control.empty():
            controls.append(self.control.get_nowait())
        frame, self.frame = self.frame, None
        return controls, frame


@dataclass
class LiveSession:
    id: str
    row: ProcessingSession
    stats: dict[str, Any] = field(default_factory=dict)
    connection: str | None = None
    subscribers: set[Subscriber] = field(default_factory=set)
    last_persist: float = 0.0
    created_monotonic: float = field(default_factory=time.monotonic)
    finishing: bool = False  # set once the session is being closed: late observations are ignored
    watchdogs: list[asyncio.Task[None]] = field(default_factory=list)
    track_states: dict[int, str] = field(default_factory=dict)  # for track audit records
    embedded_tracks: set[int] = field(default_factory=set)
    last_video_ms: int = 0


class SessionManager:
    def __init__(
        self, config: AppConfig, store: SessionStore, env: dict[str, str] | None = None
    ) -> None:
        self._config = config
        self._store = store
        self._env = env
        self.worker: WorkerLink | None = None
        self.live: LiveSession | None = None
        # Phase 3: set by the application factory when recognition is enabled.
        self.identity: IdentityManager | None = None
        # Phase 4: set by the application factory when the visit lifecycle is enabled.
        self.visits: VisitService | None = None
        self._lock = asyncio.Lock()
        self._background: set[asyncio.Task[Any]] = set()

    def attach_visits(self, service: VisitService) -> None:
        self.visits = service
        service.notify = self._notify_visit

    def _notify_visit(self, session_id: str, message: dict[str, Any]) -> None:
        live = self.live
        if live is not None and live.id == session_id:
            self._broadcast_text(live, message)

    async def run_shielded(self, coro: Any) -> None:
        """Run cleanup that must complete even if the calling task is being cancelled.

        A WebSocket handler's ``finally`` can execute inside a cancelled scope (client vanished,
        server shutting down); awaiting there would abort the cleanup half-way and leave a
        session RUNNING with no worker. The work therefore runs as its own task.
        """
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            logger.info("Cleanup continues in the background after cancellation")

    # ------------------------------------------------------------------ views
    def _sources(self) -> list[SourceDescriptor]:
        return list_sources(self._config, self._env)

    def sources_response(self) -> SourcesResponse:
        w = self.worker
        return SourcesResponse(
            sources=[SourceOut(**vars(d)) for d in self._sources()],
            worker=WorkerOut(
                connected=w is not None,
                worker_id=w.worker_id if w else None,
                model_available=w.model_available if w else None,
                model_detail=w.model_detail if w else None,
                recognition_available=w.recognition_available if w else None,
                recognition_detail=w.recognition_detail if w else None,
            ),
        )

    def source_out(self, source_id: str) -> SourceOut | None:
        d = get_descriptor(self._config, source_id, self._env)
        return SourceOut(**vars(d)) if d else None

    def to_out(self, row: ProcessingSession) -> SessionOut:
        live = self.live if self.live and self.live.id == row.id else None
        stats = dict(live.stats) if live else {}
        if not stats and row.summary:
            stats = dict(row.summary)
        if not stats:
            stats = {
                "frames_read": row.frames_read,
                "frames_processed": row.frames_processed,
                "frames_skipped": row.frames_skipped,
                "frames_dropped": row.frames_dropped,
                "corrupt_frames": row.corrupt_frames,
                "detections_total": row.detections_total,
                "max_faces_in_frame": row.max_faces_in_frame,
                "avg_inference_ms": row.avg_inference_ms or 0,
                "processing_fps": row.avg_processing_fps or 0,
            }
        snap = row.settings_snapshot or {}
        return SessionOut(
            id=row.id,
            state=row.state,
            source=SourceOut(
                id=row.source_id,
                label=row.source_label,
                type=row.source_type,
                availability="available",
            ),
            created_at=row.created_at,
            started_at=row.started_at,
            ended_at=row.ended_at,
            stop_reason=row.stop_reason,
            error=SessionError(code=row.error_code, message=row.error_message or "")
            if row.error_code
            else None,
            connection=live.connection if live else None,
            stats=SessionStats.model_validate(stats),
            settings=SessionSettings(
                model=snap.get("model", ""),
                frame_skip=snap.get("frame_skip", 0),
                confidence_threshold=snap.get("confidence_threshold", 0),
                tracking_enabled=bool(snap.get("tracking_enabled", False)),
                recognition_enabled=bool(snap.get("recognition_enabled", False)),
            )
            if snap
            else None,
        )

    async def get_session(self, session_id: str) -> SessionOut:
        row = await asyncio.to_thread(self._store.get, session_id)
        if row is None:
            raise AppError(404, "session_not_found", "No such processing session.")
        return self.to_out(row)

    async def active_session(self) -> SessionOut | None:
        if self.live is not None:
            row = await asyncio.to_thread(self._store.get, self.live.id)
            return self.to_out(row) if row else None
        return None

    async def recent_sessions(self, limit: int) -> list[SessionOut]:
        rows = await asyncio.to_thread(self._store.recent, limit)
        return [self.to_out(r) for r in rows]

    # ------------------------------------------------------------------ commands
    async def start(self, source_id: str) -> tuple[SessionOut, bool]:
        """Returns (session, created). ``created`` is False when an identical session is active."""
        desc = get_descriptor(self._config, source_id, self._env)
        if desc is None:
            raise AppError(404, "source_not_found", "Unknown video source.")
        if desc.availability != "available":
            raise AppError(409, "source_unavailable", desc.detail or "This source is unavailable.")
        async with self._lock:
            live = self.live
            if live is not None:
                row = await asyncio.to_thread(self._store.get, live.id)
                if row and row.state in ("STARTING", "RUNNING") and row.source_id == source_id:
                    return self.to_out(row), False
                raise AppError(
                    409,
                    "session_active",
                    "A processing session is already active. Stop it before starting another.",
                    {"session_id": live.id},
                )
            worker = self.worker
            if worker is None:
                raise AppError(
                    503,
                    "worker_unavailable",
                    "The video worker is not connected. "
                    "Start it (scripts\\start-worker.ps1) and retry.",
                )
            if worker.model_available is False:
                raise AppError(
                    503,
                    "model_unavailable",
                    worker.model_detail or "The face detection model is unavailable on the worker.",
                )
            d = self._config.detection
            try:
                row = await asyncio.to_thread(
                    self._store.create_active,
                    desc.id,
                    desc.label,
                    desc.type,
                    worker.worker_id,
                    {
                        "model": d.model,
                        "frame_skip": d.frame_skip,
                        "confidence_threshold": d.confidence_threshold,
                        "input_size": d.input_size,
                        "tracking_enabled": self._config.tracking.enabled,
                        "recognition_enabled": bool(
                            self.identity is not None
                            and self._config.recognition.enabled
                            and self._config.tracking.enabled
                            and worker.recognition_available
                        ),
                    },
                )
            except ActiveSessionExists as exc:
                raise AppError(
                    409, "session_active", "A processing session is already active."
                ) from exc
            live = LiveSession(id=row.id, row=row)
            self.live = live
            if self.visits is not None:
                self.visits.manager.begin_session(row.id, desc.id, desc.label)
            try:
                await worker.send(
                    {"type": P.MSG_START, "session_id": row.id, "source_id": source_id}
                )
            except Exception as exc:
                logger.warning("Could not reach worker to start session: %s", type(exc).__name__)
                await self._finish(
                    live,
                    "ERROR",
                    error_code="worker_unavailable",
                    error_message="The video worker could not be reached.",
                )
                raise AppError(
                    503, "worker_unavailable", "The video worker could not be reached."
                ) from exc
            live.watchdogs.append(asyncio.create_task(self._start_watchdog(live)))
            log_event("session.starting", session_id=row.id, source_id=source_id)
            return self.to_out(row), True

    async def stop(self, session_id: str) -> SessionOut:
        row = await asyncio.to_thread(self._store.get, session_id)
        if row is None:
            raise AppError(404, "session_not_found", "No such processing session.")
        live = self.live
        if row.state not in ACTIVE or live is None or live.id != session_id:
            if row.state in ACTIVE:  # active in the DB but unknown here: orphaned
                await asyncio.to_thread(
                    self._store.transition,
                    session_id,
                    "ERROR",
                    error_code="orphaned",
                    error_message="The session lost its worker.",
                )
                row = await asyncio.to_thread(self._store.get, session_id)
                assert row is not None
            return self.to_out(row)
        if row.state == "STOPPING":
            return self.to_out(row)
        async with self._lock:
            row = await asyncio.to_thread(
                self._store.transition, session_id, "STOPPING", stop_reason="user_requested"
            )
            live.row = row
            await self._broadcast_state(live)
            worker = self.worker
            if worker is None:
                await self._finish(live, "STOPPED", stop_reason="worker_disconnected")
            else:
                try:
                    await worker.send({"type": P.MSG_STOP, "session_id": session_id})
                    live.watchdogs.append(asyncio.create_task(self._stop_watchdog(live)))
                except Exception:
                    await self._finish(live, "STOPPED", stop_reason="worker_unreachable")
        updated = await asyncio.to_thread(self._store.get, session_id)
        assert updated is not None
        return self.to_out(updated)

    # ------------------------------------------------------------------ worker side
    async def attach_worker(self, link: WorkerLink) -> None:
        old = self.worker
        self.worker = link
        if old is not None:
            with contextlib.suppress(Exception):
                await old.ws.close(4000)
            if self.live is not None:
                await self._worker_gone("worker_replaced")

    async def detach_worker(self, link: WorkerLink) -> None:
        if self.worker is not link:
            return
        self.worker = None
        await self._worker_gone("worker_lost")

    async def _worker_gone(self, code: str) -> None:
        live = self.live
        if live is None:
            return
        row = await asyncio.to_thread(self._store.get, live.id)
        if row and row.state == "STOPPING":
            await self._finish(live, "STOPPED", stop_reason="worker_disconnected")
        else:
            await self._finish(
                live,
                "ERROR",
                error_code=code,
                error_message="The video worker disconnected while the session was active.",
            )

    async def on_worker_message(self, link: WorkerLink, message: dict[str, Any]) -> None:
        link.last_seen = time.monotonic()
        kind = message["type"]
        if kind == P.MSG_HELLO:
            link.worker_id = str(message.get("worker_id", "unknown"))[:64]
            model = message.get("model") or {}
            link.model_available = bool(model.get("available"))
            detail = model.get("detail")
            link.model_detail = str(detail)[:300] if detail else None
            recog = message.get("recognition") or {}
            link.recognition_available = bool(recog.get("available"))
            rdetail = recog.get("detail")
            link.recognition_detail = str(rdetail)[:300] if rdetail else None
            logger.info(
                "Worker registered",
                extra={"worker_id": link.worker_id, "model_available": link.model_available},
            )
            return
        live = self.live
        if kind == P.MSG_HEARTBEAT:
            reported = message.get("active_session_id")
            if (
                live is not None
                and reported != live.id
                and time.monotonic() - live.created_monotonic
                > 3 * self._config.worker.heartbeat_interval_seconds
            ):
                row = await asyncio.to_thread(self._store.get, live.id)
                if row and row.state in ("STARTING", "RUNNING"):
                    await self._finish(
                        live,
                        "ERROR",
                        error_code="worker_lost_session",
                        error_message="The worker is no longer processing this session.",
                    )
            return
        sid = message.get("session_id")
        if live is None or sid != live.id:
            logger.debug("Ignoring %s for unknown session", kind)
            return
        if kind == P.MSG_ACCEPTED:
            return
        if kind == P.MSG_STATE:
            state = message.get("state")
            if state == "running":
                await self._on_running(live)
            elif state in ("reconnecting", "connected"):
                live.connection = state
                if self.visits is not None:
                    self.visits.manager.on_connection(live.id, state)
                audit(f"source.{state}", session_id=live.id)
                await self._broadcast_state(live)
        elif kind == P.MSG_STATS:
            stats = message.get("stats")
            if isinstance(stats, dict):
                live.stats = stats
                self._broadcast_text(live, {"type": "stats", "stats": stats})
                now = time.monotonic()
                if now - live.last_persist >= STATS_PERSIST_INTERVAL:
                    live.last_persist = now
                    await asyncio.to_thread(self._store.save_stats, live.id, stats)
        elif kind == P.MSG_ENDED:
            await self._on_ended(live, message)

    async def on_worker_frame(self, link: WorkerLink, data: bytes) -> None:
        link.last_seen = time.monotonic()
        live = self.live
        if live is None:
            return
        try:
            header, payload = P.unpack_frame(data, require_jpeg=False)
        except P.ProtocolError as exc:
            logger.warning("Dropping malformed worker frame: %s", exc)
            return
        if header.get("kind") == P.KIND_OBSERVATION:
            await self._on_observation(live, header, payload)
            return
        if not payload:
            return
        if header.get("session_id") != live.id:
            return
        if header.get("tracks"):
            # The API never tracks, but it does refuse malformed track metadata so a buggy worker
            # cannot feed the browser inconsistent data.
            try:
                TrackingFrameMeta.model_validate(header)
            except ValidationError as exc:
                logger.warning(
                    "Dropping frame: invalid tracking metadata (%d errors)", exc.error_count()
                )
                return
        if "tracks" in header and not live.finishing:
            self._visit_frame(live, header)
        for sub in list(live.subscribers):
            sub.push_frame(data)

    async def _on_observation(self, live: LiveSession, header: dict[str, Any], crop: bytes) -> None:
        """Recognition observation from the worker -> identity decision -> browser events."""
        identity = self.identity
        if identity is None or live.finishing or header.get("session_id") != live.id:
            return  # unknown/old session (delayed message) or recognition not configured
        try:
            obs = parse_observation(header, crop, identity.dim)
        except ValueError as exc:
            logger.warning("Dropping invalid observation: %s", exc)
            return
        visits = self.visits
        if visits is not None and obs.embedding is not None:
            # Only observations that passed the recognition quality gate can become evidence.
            visits.manager.on_observation(
                live.id, obs.track_id, obs.crop_jpeg, obs.quality, obs.timestamp_ms
            )
            if obs.track_id not in live.embedded_tracks:
                live.embedded_tracks.add(obs.track_id)
                audit(
                    "embedding.generated", session_id=live.id, track_id=obs.track_id,
                    model_version=obs.model_version, quality=round(obs.quality, 3),
                )  # fmt: skip
        try:
            events = await asyncio.to_thread(identity.observe, obs)
        except Exception:
            logger.exception("Identity resolution failed")
            return
        for event in events:
            self._broadcast_text(live, event.to_message())
            if event.kind in ("registered", "recognized", "ambiguous", "failed"):
                log_event(
                    f"identity.{event.kind}",
                    session_id=live.id,
                    track_id=event.track_id,
                    visitor_number=event.visitor_number,
                    reason=event.reason,
                )
                audit(
                    "visitor.registered" if event.kind == "registered" else f"recognition.{event.kind}",
                    session_id=live.id, track_id=event.track_id, visitor_id=event.visitor_id,
                    visitor_label=None if event.visitor_number is None else visitor_label(event.visitor_number),
                    similarity=event.similarity, reason=event.reason,
                )  # fmt: skip
            if visits is not None:
                self._visit_identity(live, event, obs.timestamp_ms)

    # ------------------------------------------------------------------ visit lifecycle (Phase 4)
    def _visit_frame(self, live: LiveSession, header: dict[str, Any]) -> None:
        visits = self.visits
        if visits is None:
            return
        try:
            video_ms = int(header["timestamp_ms"])
            frame_id = int(header["frame_id"])
            raw_tracks = header.get("tracks") or []
            snaps = [
                TrackSnap(
                    int(t["track_id"]),
                    str(t["state"]),
                    bool(t["observed"]),
                    int(t["first_seen_ms"]),
                )
                for t in raw_tracks
            ]
        except (KeyError, TypeError, ValueError):
            logger.warning("Frame without usable timing for the visit lifecycle")
            return
        if video_ms < 0 or frame_id < 0:
            return
        live.last_video_ms = max(live.last_video_ms, video_ms)
        for t in snaps:  # low-rate audit trail of track creation / recovery
            previous = live.track_states.get(t.track_id)
            if previous is None:
                audit("track.created", session_id=live.id, track_id=t.track_id)
            if t.state == "CONFIRMED" and previous in (None, "TENTATIVE"):
                audit("face.detected", session_id=live.id, track_id=t.track_id)
            elif t.state == "CONFIRMED" and previous == "LOST":
                audit("track.recovered", session_id=live.id, track_id=t.track_id)
            live.track_states[t.track_id] = t.state
        visits.submit(
            visits.manager.on_frame(live.id, frame_id, video_ms, datetime.now(UTC), snaps)
        )

    def _visit_identity(self, live: LiveSession, event: Any, video_ms: int) -> None:
        visits = self.visits
        if visits is None:
            return
        if event.visitor_id is not None:
            visitor, number = event.visitor_id, event.visitor_number
        elif event.state in ("AMBIGUOUS", "REJECTED"):
            visitor, number = None, None
        else:
            return
        visits.submit(
            visits.manager.on_identity(
                live.id,
                event.track_id,
                visitor,
                number,
                max(video_ms, live.last_video_ms),
                datetime.now(UTC),
            )
        )

    def visits_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        return self.visits.snapshot(session_id) if self.visits is not None else []

    @staticmethod
    def _closure_reason(state: str, stop_reason: str | None, error_code: str | None) -> str:
        """How the end of a session maps onto its open visits (see docs/phase-4-report.md)."""
        if state == "COMPLETED":
            return "end_of_stream"
        if state == "STOPPED":
            return (
                "manual_stop"
                if stop_reason in (None, "user_requested", "stopped")
                else "worker_lost"
            )
        if error_code == "api_shutdown":
            return "api_shutdown"
        if error_code in ("worker_lost", "worker_replaced", "worker_lost_session", "orphaned"):
            return "worker_lost"
        return "source_error"

    def identity_snapshot(self, session_id: str) -> list[dict[str, Any]]:
        if self.identity is None:
            return []
        return [e.to_message() for e in self.identity.snapshot(session_id)]

    async def _on_running(self, live: LiveSession) -> None:
        row = await asyncio.to_thread(self._store.get, live.id)
        if row is None or row.state != "STARTING":
            return
        live.row = await asyncio.to_thread(self._store.transition, live.id, "RUNNING")
        live.connection = live.connection or "connected"
        log_event("session.running", session_id=live.id)
        await self._broadcast_state(live)

    async def _on_ended(self, live: LiveSession, message: dict[str, Any]) -> None:
        state = message.get("state")
        stats = message.get("stats") if isinstance(message.get("stats"), dict) else None
        if stats:
            live.stats = stats
        if state == "ERROR":
            await self._finish(
                live,
                "ERROR",
                error_code=str(message.get("code") or "processing_error"),
                error_message=str(message.get("message") or "Processing failed."),
                stats=stats,
            )
        elif state == "COMPLETED":
            await self._finish(live, "COMPLETED", stop_reason="end_of_stream", stats=stats)
        else:
            row = await asyncio.to_thread(self._store.get, live.id)
            reason = row.stop_reason if row and row.stop_reason else "stopped"
            await self._finish(live, "STOPPED", stop_reason=reason, stats=stats)

    async def _finish(
        self,
        live: LiveSession,
        state: str,
        *,
        stop_reason: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        if self.live is not live:
            return
        live.finishing = True
        if self.visits is not None:
            # Open visits end here, by an explicit rule (EOF / manual stop => observation ended;
            # crash / shutdown / source failure => interrupted). The events are persisted BEFORE the
            # session is marked finished, so anybody who sees the final state also sees them.
            reason = self._closure_reason(state, stop_reason, error_code)
            self.visits.submit(self.visits.manager.end_session(live.id, reason, datetime.now(UTC)))
            await self.visits.drain(self._config.visits.drain_timeout_seconds)
        if self.identity is not None:
            # Identity associations are persisted BEFORE the session is marked finished, so anybody
            # who sees COMPLETED/STOPPED/ERROR also sees what recognition concluded.
            await asyncio.to_thread(self.identity.end_session, live.id)
        for task in live.watchdogs:
            if task is not asyncio.current_task():
                task.cancel()
        try:
            row = await asyncio.to_thread(
                self._store.transition,
                live.id,
                state,
                stop_reason=stop_reason,
                error_code=error_code,
                error_message=error_message,
                stats=stats or live.stats,
            )
        except InvalidTransition:  # already terminal
            existing = await asyncio.to_thread(self._store.get, live.id)
            if existing is None:
                self.live = None
                return
            row = existing
        live.row = row
        self.live = None
        # Final view (state + stats) goes to subscribers before they are released.
        out = self.to_out(row) if row else None
        for sub in list(live.subscribers):
            if out is not None:
                sub.push_control(
                    P.encode_control({"type": "ended", "session": out.model_dump(mode="json")})
                )
        log_event(
            f"session.{state.lower()}",
            session_id=live.id,
            error_code=error_code,
            stop_reason=stop_reason,
        )

    # ------------------------------------------------------------------ watchdogs
    async def _start_watchdog(self, live: LiveSession) -> None:
        await asyncio.sleep(self._config.worker.start_timeout_seconds)
        row = await asyncio.to_thread(self._store.get, live.id)
        if self.live is live and row and row.state == "STARTING":
            worker = self.worker
            if worker is not None:
                with contextlib.suppress(Exception):
                    await worker.send({"type": P.MSG_STOP, "session_id": live.id})
            await self._finish(
                live,
                "ERROR",
                error_code="start_timeout",
                error_message="The source did not start in time.",
            )

    async def _stop_watchdog(self, live: LiveSession) -> None:
        await asyncio.sleep(self._config.worker.stop_timeout_seconds)
        if self.live is live:
            await self._finish(live, "STOPPED", stop_reason="stop_timeout")

    async def monitor_worker(self) -> None:
        """Declare the worker lost if its heartbeat stops (half-open connections)."""
        interval = self._config.worker.heartbeat_interval_seconds
        while True:
            await asyncio.sleep(interval)
            link = self.worker
            if link is not None and time.monotonic() - link.last_seen > 3 * interval:
                logger.warning("Worker heartbeat timeout")
                with contextlib.suppress(Exception):
                    await link.ws.close(4001)
                await self.detach_worker(link)

    # ------------------------------------------------------------------ subscribers
    def _broadcast_text(self, live: LiveSession, message: dict[str, Any]) -> None:
        text = P.encode_control(message)
        for sub in list(live.subscribers):
            sub.push_control(text)

    async def _broadcast_state(self, live: LiveSession) -> None:
        row = await asyncio.to_thread(self._store.get, live.id)
        if row is not None:
            self._broadcast_text(
                live, {"type": "state", "session": self.to_out(row).model_dump(mode="json")}
            )

    async def subscribe(self, session_id: str) -> tuple[Subscriber | None, SessionOut]:
        """Attach to the active session, or just return the stored view for a finished one."""
        out = await self.get_session(session_id)
        live = self.live
        if live is not None and live.id == session_id:
            sub = Subscriber()
            live.subscribers.add(sub)
            return sub, out
        return None, out

    def unsubscribe(self, session_id: str, sub: Subscriber) -> None:
        sub.closed = True
        if self.live is not None and self.live.id == session_id:
            self.live.subscribers.discard(sub)

    # ------------------------------------------------------------------ lifecycle
    async def reconcile_orphans(self) -> int:
        n = await asyncio.to_thread(
            self._store.fail_orphans,
            "api_restarted",
            "The API restarted while this session was active.",
        )
        if n:
            logger.warning("Marked %d orphaned session(s) as ERROR", n)
        return n

    async def shutdown(self) -> None:
        live = self.live
        if live is not None:
            worker = self.worker
            if worker is not None:
                with contextlib.suppress(Exception):
                    await worker.send({"type": P.MSG_STOP, "session_id": live.id})
            await self._finish(
                live,
                "ERROR",
                error_code="api_shutdown",
                error_message="The API shut down while the session was active.",
            )
