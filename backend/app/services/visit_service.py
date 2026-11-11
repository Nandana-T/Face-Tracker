"""Asynchronous glue: lifecycle state machine -> bounded queue -> durable recorder -> live events.

The frame-processing path only runs the in-memory ``VisitManager`` (microseconds) and enqueues
actions. Database, filesystem and log work happens in ONE consumer task (``asyncio.to_thread``), so
events keep their order (entry before exit) and a slow disk or database can never stall frames.
A failed write is retried with exponential backoff; after ``max_attempts`` the event is reported as a
persistence failure (it stays in the dead-letter counter and the log) instead of being dropped silently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Callable
from typing import Any

from app.core.config import VisitsConfig
from app.core.events import audit
from app.services.event_recorder import EventConflict, EventRecorder, OrderViolation
from app.services.visit_manager import VisitAction, VisitManager

logger = logging.getLogger("katomaran.visits")
MAINTENANCE_SECONDS = 30.0

Notify = Callable[[str, dict[str, Any]], None]


class VisitService:
    def __init__(self, cfg: VisitsConfig, recorder: EventRecorder) -> None:
        self.cfg = cfg
        self.recorder = recorder
        self.manager = VisitManager(cfg)
        self.notify: Notify = lambda _sid, _msg: None
        self.stats: Counter[str] = Counter()
        self._queue: asyncio.Queue[VisitAction] = asyncio.Queue(maxsize=cfg.queue_size)
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._consume()),
                asyncio.create_task(self._maintain()),
            ]

    async def stop(self, drain_seconds: float = 0) -> None:
        if drain_seconds > 0:
            await self.drain(drain_seconds)
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []

    async def drain(self, timeout: float) -> bool:  # noqa: ASYNC109 - plain seconds, applied below
        """Wait until every queued action has been processed (or ``timeout`` elapsed)."""
        try:
            await asyncio.wait_for(self._queue.join(), timeout)
            return True
        except TimeoutError:
            return False

    # ------------------------------------------------------------------ input
    def submit(self, actions: list[VisitAction]) -> None:
        for action in actions:
            if action.kind == "state":
                self.notify(action.visit.session_id, self._state_message(action))
                continue
            try:
                self._queue.put_nowait(action)
            except asyncio.QueueFull:
                if action.kind == "touch":
                    continue  # best effort
                self.stats["queue_overflow"] += 1
                audit(
                    "persistence.failure", logging.ERROR, visit_id=action.visit.id,
                    kind=action.kind, reason="queue_overflow",
                )  # fmt: skip
                logger.error("Visit event queue is full: an event was NOT recorded")

    @staticmethod
    def _state_message(action: VisitAction) -> dict[str, Any]:
        v = action.visit
        return {
            "type": "visit_state",
            "visit_id": v.id,
            "visitor_id": v.visitor_id,
            "visitor_number": v.visitor_number,
            "state": v.state,
        }

    # ------------------------------------------------------------------ consumer
    async def _consume(self) -> None:
        while True:
            action = await self._queue.get()
            try:
                await self._process(action)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad action must not kill the consumer
                logger.exception("Unexpected failure while recording a visit event")
            finally:
                self._queue.task_done()

    async def _process(self, action: VisitAction) -> None:
        snap = action.visit
        if action.kind == "touch":
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    self.recorder.touch,
                    snap.id,
                    snap.last_wall,
                    snap.last_video_ms,
                    snap.track_segments,
                )
            return
        delay = self.cfg.retry_base_seconds
        for attempt in range(1, self.cfg.max_attempts + 1):
            try:
                result = await asyncio.to_thread(self.recorder.record, action)
                break
            except (EventConflict, OrderViolation) as exc:
                self.stats["rejected"] += 1
                audit(
                    "persistence.rejected", logging.ERROR, visit_id=snap.id, kind=action.kind,
                    reason=type(exc).__name__,
                )  # fmt: skip
                return
            except Exception as exc:  # noqa: BLE001 - database outage, disk problems, ...
                self.stats["retries"] += 1
                audit(
                    "persistence.retry", logging.WARNING, visit_id=snap.id, kind=action.kind,
                    attempt=attempt, error=type(exc).__name__,
                )  # fmt: skip
                if attempt == self.cfg.max_attempts:
                    self.stats["dead_letters"] += 1
                    audit(
                        "persistence.failure", logging.ERROR, visit_id=snap.id, kind=action.kind,
                        reason="max_attempts_exceeded",
                    )  # fmt: skip
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.cfg.retry_max_seconds)
        else:  # pragma: no cover - loop always breaks or returns
            return
        if action.kind == "terminal":
            self.manager.acknowledge(snap.session_id, snap.id)
        if result.created:
            message = await asyncio.to_thread(self.recorder.describe, result.event_id)
            if message is not None:
                self.notify(snap.session_id, {"type": "visit_event", "event": message})

    # ------------------------------------------------------------------ maintenance
    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_SECONDS)
            try:
                await asyncio.to_thread(self.recorder.retry_images)
                await asyncio.to_thread(self.recorder.deliver_due)
            except Exception:  # noqa: BLE001
                logger.warning("Event maintenance pass failed", exc_info=True)

    def snapshot(self, session_id: str) -> list[dict[str, Any]]:
        return [
            {
                "visit_id": v.id,
                "visitor_id": v.visitor_id,
                "visitor_number": v.visitor_number,
                "state": v.state,
            }
            for v in self.manager.active(session_id)
        ]

    def runtime_stats(self) -> dict[str, Any]:
        return {**self.manager.stats, **self.stats, "queue_depth": self._queue.qsize()}
