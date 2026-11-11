"""Durable, idempotent creation of visit entry/exit events with recoverable side effects.

Guarantee (and its limits). Exactly-once delivery across PostgreSQL, a directory of JPEGs and a
plain log file cannot be atomic, so this module provides *idempotent logical event creation with
recoverable side-effect delivery*:

  1. evidence image   written atomically under a name derived from the stable event id
                      (a retry rewrites nothing; a crash before step 2 leaves an ORPHAN file that
                      reconciliation removes - or that the retried event adopts);
  2. database         ONE transaction: visit row (entry), event row, outbox row. Unique constraints
                      (visit_id, slot) and idempotency_key make a repeat return the existing event
                      (identical payload = duplicate) or raise ``EventConflict`` (different payload);
  3. events.log       delivered AFTER the commit from the outbox. The attempt counter is committed
                      BEFORE the line is written, so a crash between write and acknowledgement shows
                      up as a second line with ``delivery_attempt`` = 2 - an explained redelivery,
                      never an unexplained duplicate business event.

PostgreSQL is the source of truth; the log file and the image are delivery artifacts. When evidence
cannot be recovered the event stays and says so (``image_status`` MISSING/UNAVAILABLE) - evidence is
never fabricated. The one deliberate substitution is the exit image: it is the LAST GOOD observation
(or, if none, the entry image, flagged ``entry_representative``), never a newly captured face.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import VisitsConfig
from app.core.events import EventsLogError, EventsLogWriter, audit
from app.models.visit import EventDeliveryOutbox, Visit, VisitEvent
from app.models.visitor import Visitor
from app.services.evidence_store import EvidenceRejected, EvidenceStore, EvidenceWriteError
from app.services.identity_manager import visitor_label
from app.services.image_store import ImageStoreError
from app.services.visit_manager import VisitAction, event_id_for

logger = logging.getLogger("katomaran.events.recorder")
TARGET = "events_log"
LIVE_STATES = ("ACTIVE", "TEMPORARILY_MISSING", "EXIT_PENDING")


class EventConflict(Exception):
    """The same idempotency key was presented with a different payload."""


class OrderViolation(Exception):
    """A terminal event for a visit that has no recorded entry."""


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass
class RecordResult:
    event_id: str
    visit_id: str
    event_type: str
    created: bool
    duplicate: bool
    image_status: str
    delivered: bool
    db_ms: float = 0.0
    image_ms: float = 0.0
    log_ms: float = 0.0


class _Timing:
    __slots__ = ("count", "max", "total")

    def __init__(self) -> None:
        self.total = 0.0
        self.count = 0
        self.max = 0.0

    def add(self, ms: float) -> None:
        self.total += ms
        self.count += 1
        self.max = max(self.max, ms)

    def summary(self) -> dict[str, float]:
        return {
            "count": self.count,
            "avg_ms": round(self.total / self.count, 3) if self.count else 0.0,
            "max_ms": round(self.max, 3),
        }


class EventRecorder:
    def __init__(
        self,
        factory: sessionmaker[Session],
        store: EvidenceStore,
        cfg: VisitsConfig,
        writer: EventsLogWriter | None = None,
    ) -> None:
        self._factory = factory
        self.store = store
        self.cfg = cfg
        self._writer = writer or EventsLogWriter()
        self.stats: Counter[str] = Counter()
        self.timing = {k: _Timing() for k in ("db", "image", "log")}
        # Evidence that could not be written yet (storage unavailable): retried in-process only.
        self._image_retry: dict[str, tuple[str, bytes]] = {}
        self.last_reconcile: dict[str, Any] | None = None

    # ================================================================== recording
    def record(self, action: VisitAction, now: datetime | None = None) -> RecordResult:
        """Create (or find) the entry/terminal event for ``action`` and deliver its log line."""
        if action.kind not in ("entry", "terminal"):
            raise ValueError("only entry and terminal actions are recorded")
        snap = action.visit
        entry = action.kind == "entry"
        slot = "entry" if entry else "terminal"
        etype = "entry" if entry else (action.event_type or "exit")
        event_id = event_id_for(snap.id, slot)
        when = aware(snap.entry_wall if entry else snap.last_wall)
        video_ms = snap.entry_video_ms if entry else snap.last_video_ms
        now = now or datetime.now(UTC)

        t0 = time.perf_counter()
        known = self._visitor_exists(snap.visitor_id)
        if known:
            rel, sha, image_status, image_kind, image_ms_pos = self._store_evidence(
                action, etype, event_id, when, entry
            )
        else:
            # The visitor was erased while the visit was open: nothing biometric may be stored for them
            # after the erasure; the event is still recorded, de-identified.
            rel, sha, image_status, image_kind, image_ms_pos = None, None, "ERASED", None, None
            self.stats["events_for_erased_visitors"] += 1
        self.timing["image"].add((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        payload = hashlib.sha256(
            f"{snap.id}|{etype}|{snap.visitor_id}|{when.isoformat()}".encode()
        ).hexdigest()
        created = self._commit(
            action, etype, slot, event_id, when, video_ms, now, payload,
            (rel, sha, image_status, image_kind, image_ms_pos), known,
        )  # fmt: skip
        db_ms = (time.perf_counter() - t1) * 1000
        self.timing["db"].add(db_ms)
        self.stats["events_created" if created else "duplicates_ignored"] += 1

        t2 = time.perf_counter()
        delivered = self.deliver(event_id)
        log_ms = (time.perf_counter() - t2) * 1000
        self.timing["log"].add(log_ms)
        with self._factory() as db:
            ev = db.get(VisitEvent, event_id)
            final_image = ev.image_status if ev else image_status
        return RecordResult(
            event_id, snap.id, etype, created, not created, final_image, delivered, db_ms,
            (time.perf_counter() - t0) * 1000 - db_ms - log_ms, log_ms,
        )  # fmt: skip

    def _store_evidence(
        self, action: VisitAction, etype: str, event_id: str, when: datetime, entry: bool
    ) -> tuple[str | None, str | None, str, str | None, int | None]:
        crop = action.crop
        rel = self.store.relative_path(etype, event_id, when)
        kind = action.image_kind
        position = crop.video_ms if crop else None
        jpeg = crop.jpeg if crop else None
        if jpeg is None and not entry:
            fallback = self._entry_image(action.visit.id)
            if fallback is not None:
                jpeg, kind, position = fallback, "entry_representative", None
        if jpeg is None:
            return None, None, "UNAVAILABLE", None, None
        try:
            sha = self.store.save(rel, jpeg)
            return rel, sha, "STORED", kind, position
        except EvidenceRejected:
            # Blank / corrupt / tiny: never valid evidence, retrying cannot help - say so, store nothing.
            self.stats["evidence_rejected"] += 1
            return None, None, "UNAVAILABLE", None, None
        except EvidenceWriteError:
            # Storage failure: keep the bytes in memory and retry; if the process dies first,
            # reconciliation reports the evidence as MISSING (never fabricated).
            self.stats["evidence_write_failed"] += 1
            self._image_retry[event_id] = (rel, jpeg)
            return rel, None, "PENDING", kind, position

    def _visitor_exists(self, visitor_id: str | None) -> bool:
        if not visitor_id:
            return False
        with self._factory() as db:
            return db.get(Visitor, visitor_id) is not None

    def _entry_image(self, visit_id: str) -> bytes | None:
        try:
            with self._factory() as db:
                path = db.execute(
                    select(VisitEvent.image_path).where(
                        VisitEvent.visit_id == visit_id,
                        VisitEvent.slot == "entry",
                        VisitEvent.image_status == "STORED",
                    )
                ).scalar_one_or_none()
            return self.store.read(path) if path else None
        except Exception:  # noqa: BLE001 - a fallback must never break recording
            return None

    def _commit(
        self,
        action: VisitAction,
        etype: str,
        slot: str,
        event_id: str,
        when: datetime,
        video_ms: int,
        now: datetime,
        payload: str,
        image: tuple[str | None, str | None, str, str | None, int | None],
        visitor_known: bool = True,
    ) -> bool:
        snap = action.visit
        entry = slot == "entry"
        key = f"{snap.visit_key}:{slot}"
        rel, sha, image_status, image_kind, image_pos = image
        with self._factory() as db:
            try:
                existing = db.execute(
                    select(VisitEvent).where(VisitEvent.idempotency_key == key)
                ).scalar_one_or_none()
                if existing is not None:
                    return self._same_or_conflict(existing, payload)
                visit = db.get(Visit, snap.id)
                if entry:
                    if visit is None:
                        visit = Visit(
                            id=snap.id, visit_key=snap.visit_key,
                            visitor_id=snap.visitor_id if visitor_known else None,
                            visitor_number=snap.visitor_number if visitor_known else None,
                            session_id=snap.session_id,
                            source_id=snap.source_id, source_label=snap.source_label,
                            state="ACTIVE", entry_time=when, entry_video_ms=video_ms,
                            last_observed_time=when, last_observed_video_ms=video_ms,
                            track_segments=snap.track_segments,
                        )  # fmt: skip
                        db.add(visit)
                        db.flush()
                else:
                    has_entry = (
                        visit is not None
                        and db.execute(
                            select(VisitEvent.id).where(
                                VisitEvent.visit_id == snap.id, VisitEvent.slot == "entry"
                            )
                        ).first()
                        is not None
                    )
                    if visit is None or not has_entry:
                        self.stats["order_violations"] += 1
                        raise OrderViolation("terminal event without a recorded entry")
                    final = "INTERRUPTED" if etype == "interrupted" else "CLOSED"
                    exit_time = max(when, aware(visit.entry_time))
                    visit.state = final
                    visit.exit_time = exit_time
                    visit.exit_video_ms = video_ms
                    visit.exit_confirmed_at = now
                    visit.exit_basis = snap.exit_basis
                    visit.closure_reason = snap.closure_reason
                    visit.last_observed_time = exit_time
                    visit.last_observed_video_ms = video_ms
                    visit.track_segments = max(visit.track_segments, snap.track_segments)
                # An event follows its visit's identity (a visit de-identified by an erasure stays so).
                owner = visit.visitor_id if visit is not None else None
                db.add(
                    VisitEvent(
                        id=event_id, visit_id=snap.id, visitor_id=owner,
                        visitor_number=snap.visitor_number if owner else None,
                        event_type=etype, slot=slot,
                        idempotency_key=key, payload_hash=payload, event_time=when,
                        video_ms=video_ms, recorded_at=now, image_status=image_status,
                        image_path=rel, image_sha256=sha, image_kind=image_kind,
                        image_video_ms=image_pos, persistence_status="PENDING",
                    )
                )  # fmt: skip
                db.flush()  # no ORM relationships: make the insert order (event before outbox) explicit
                db.add(
                    EventDeliveryOutbox(
                        id=uuid.uuid4().hex, event_id=event_id, target=TARGET, status="PENDING",
                        attempts=0, next_retry_at=now,
                    )
                )  # fmt: skip
                db.commit()
                return True
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(VisitEvent).where(VisitEvent.idempotency_key == key)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                return self._same_or_conflict(existing, payload)
            except Exception:
                db.rollback()
                raise

    def _same_or_conflict(self, existing: VisitEvent, payload: str) -> bool:
        if existing.payload_hash == payload:
            return False  # identical retry: the logical event already exists
        self.stats["conflicts"] += 1
        audit(
            "event.conflict", logging.WARNING, event_id=existing.id, visit_id=existing.visit_id,
            reason="same idempotency key, different payload",
        )  # fmt: skip
        raise EventConflict(f"conflicting payload for event {existing.id}")

    # ================================================================== delivery (events.log)
    def deliver(self, event_id: str, now: datetime | None = None) -> bool:
        """Write the events.log line for a committed event. True when the outbox row is DELIVERED."""
        now = now or datetime.now(UTC)
        with self._factory() as db:
            ob = db.execute(
                select(EventDeliveryOutbox).where(
                    EventDeliveryOutbox.event_id == event_id, EventDeliveryOutbox.target == TARGET
                )
            ).scalar_one_or_none()
            if ob is None or ob.status == "DELIVERED":
                return ob is not None
            # Claim = compare-and-swap on the attempt counter, committed BEFORE writing: exactly one
            # of several concurrent deliverers wins, and a crash after the write shows up as a second
            # line with a higher ``delivery_attempt`` (an explained redelivery). The lease keeps the
            # retry sweep away from a delivery that is in progress.
            attempt = ob.attempts + 1
            claimed = db.execute(
                update(EventDeliveryOutbox)
                .where(
                    EventDeliveryOutbox.id == ob.id,
                    EventDeliveryOutbox.status == "PENDING",
                    EventDeliveryOutbox.attempts == ob.attempts,
                )
                .values(attempts=attempt, next_retry_at=now + timedelta(seconds=30))
            )
            db.commit()
            if getattr(claimed, "rowcount", 0) != 1:
                self.stats["delivery_claims_lost"] += 1
                return False
            db.refresh(ob)
            ev = db.get(VisitEvent, event_id)
            visit = db.get(Visit, ev.visit_id) if ev else None
            if ev is None or visit is None:
                return False
            try:
                self._writer.write(f"visit.{ev.event_type}", **self._log_fields(ev, visit, attempt))
            except Exception as exc:  # noqa: BLE001
                self.stats["log_delivery_failures"] += 1
                ob.last_error = type(exc).__name__[:200]
                delay = min(self.cfg.retry_max_seconds, self.cfg.retry_base_seconds * 2**attempt)
                ob.next_retry_at = now + timedelta(seconds=delay)
                if attempt >= self.cfg.max_attempts:
                    ob.status = "DEAD"
                    ev.persistence_status = "DEAD"
                    self.stats["outbox_dead"] += 1
                db.commit()
                logger.error(
                    "events.log delivery failed (%s), attempt %d", type(exc).__name__, attempt
                )
                return False
            ob.status = "DELIVERED"
            ob.delivered_at = now
            ob.last_error = None
            ev.persistence_status = "DELIVERED"
            db.commit()
            if attempt > 1:
                self.stats["redeliveries"] += 1
            return True

    @staticmethod
    def _log_fields(ev: VisitEvent, visit: Visit, attempt: int) -> dict[str, Any]:
        return {
            "event_id": ev.id,
            "visit_id": ev.visit_id,
            "visitor_id": ev.visitor_id,
            "visitor_label": visitor_label(ev.visitor_number) if ev.visitor_number else None,
            "event_type": ev.event_type,
            "event_time": aware(ev.event_time).isoformat(timespec="milliseconds"),
            "video_ms": ev.video_ms,
            "session_id": visit.session_id,
            "source_id": visit.source_id,
            "source_label": visit.source_label,
            "image_ref": ev.image_path,
            "image_status": ev.image_status,
            "image_kind": ev.image_kind,
            "persistence": "committed",
            "delivery_attempt": attempt,
            "exit_basis": visit.exit_basis if ev.slot == "terminal" else None,
            "closure_reason": visit.closure_reason if ev.slot == "terminal" else None,
        }

    def describe(self, event_id: str) -> dict[str, Any] | None:
        """The live-UI view of one event (no paths, no biometrics)."""
        with self._factory() as db:
            ev = db.get(VisitEvent, event_id)
            visit = db.get(Visit, ev.visit_id) if ev else None
            if ev is None or visit is None:
                return None
            return {
                "id": ev.id,
                "visit_id": ev.visit_id,
                "type": ev.event_type,
                "visitor": None
                if ev.visitor_id is None
                else {
                    "id": ev.visitor_id,
                    "number": ev.visitor_number,
                    "label": visitor_label(ev.visitor_number or 0),
                },
                "event_time": aware(ev.event_time).isoformat(timespec="milliseconds"),
                "session_id": visit.session_id,
                "source_label": visit.source_label,
                "image_status": ev.image_status,
                "persistence_status": ev.persistence_status,
                "exit_basis": visit.exit_basis if ev.slot == "terminal" else None,
                "closure_reason": visit.closure_reason if ev.slot == "terminal" else None,
            }

    # ================================================================== maintenance
    def touch(self, visit_id: str, last_wall: datetime, last_video_ms: int, segments: int) -> None:
        """Persist the last observation of an OPEN visit (throttled by the caller; monotonic)."""
        with self._factory() as db:
            db.execute(
                update(Visit)
                .where(
                    Visit.id == visit_id,
                    Visit.state.in_(LIVE_STATES),
                    Visit.last_observed_time <= aware(last_wall),
                )
                .values(
                    last_observed_time=aware(last_wall),
                    last_observed_video_ms=last_video_ms,
                    track_segments=segments,
                )
            )
            db.commit()

    def retry_images(self) -> int:
        """Write evidence that previously failed (storage was unavailable). Returns how many succeeded."""
        done = 0
        for event_id, (rel, jpeg) in list(self._image_retry.items()):
            try:
                sha = self.store.save(rel, jpeg)
            except ImageStoreError:
                continue
            with self._factory() as db:
                ev = db.get(VisitEvent, event_id)
                if ev is not None and ev.image_status == "PENDING":
                    ev.image_status, ev.image_sha256 = "STORED", sha
                    db.commit()
            del self._image_retry[event_id]
            done += 1
        return done

    def deliver_due(self, limit: int = 100, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        with self._factory() as db:
            ids = [
                i
                for (i,) in db.execute(
                    select(EventDeliveryOutbox.event_id)
                    .where(
                        EventDeliveryOutbox.status == "PENDING",
                        EventDeliveryOutbox.next_retry_at <= now,
                    )
                    .order_by(EventDeliveryOutbox.created_at)
                    .limit(limit)
                )
            ]
        return sum(1 for i in ids if self.deliver(i, now))

    def reconcile(
        self, live_sessions: set[str] | None = None, startup: bool = False
    ) -> dict[str, int]:
        """Restore consistency after a crash/outage. Safe to run repeatedly and concurrently with
        normal operation (it only touches things that are provably stale). ``startup=True`` means no
        delivery can be in progress, so back-off and in-flight leases are ignored."""
        live_sessions = live_sessions or set()
        result = Counter[str]()
        now = datetime.now(UTC)
        result["images_retried"] = self.retry_images()
        result["log_lines_delivered"] = self.deliver_due(
            limit=1000, now=now + timedelta(days=1) if startup else now
        )
        with self._factory() as db:
            referenced: set[str] = set()
            rows = db.execute(
                select(VisitEvent).where(VisitEvent.image_path.is_not(None))
            ).scalars()
            for ev in rows:
                referenced.add(ev.image_path or "")
                exists = self.store.exists(ev.image_path or "")
                if ev.image_status == "PENDING":
                    if exists:
                        ev.image_status, ev.image_sha256 = (
                            "STORED",
                            self.store.sha256(ev.image_path or ""),
                        )
                        result["images_adopted"] += 1
                    elif ev.id not in self._image_retry:
                        ev.image_status = "MISSING"
                        result["evidence_missing"] += 1
                elif ev.image_status == "STORED" and not exists:
                    ev.image_status = "MISSING"
                    result["evidence_missing"] += 1
            db.commit()
            for rel, mtime in self.store.all_files():
                if rel in referenced or (
                    rel.endswith(".part") and self.store.age_seconds(mtime) < 60
                ):
                    continue
                if self.store.age_seconds(mtime) > self.cfg.orphan_grace_seconds:
                    self.store.delete_any(rel)
                    result["orphan_files_removed"] += 1
            # Evidence of erased visitors that survived an interrupted erasure.
            for ev in db.execute(
                select(VisitEvent).where(
                    VisitEvent.visitor_id.is_(None), VisitEvent.image_path.is_not(None)
                )
            ).scalars():
                self.store.delete_any(ev.image_path or "")
                ev.image_path, ev.image_sha256, ev.image_status = None, None, "ERASED"
                result["erasure_completed"] += 1
            db.commit()
            stale = db.execute(select(Visit).where(Visit.state.in_(LIVE_STATES))).scalars().all()
            stale_ids = [v.id for v in stale if v.session_id not in live_sessions]
        for visit_id in stale_ids:
            if self._interrupt_stale(visit_id, now):
                result["visits_interrupted"] += 1
        self.last_reconcile = {**result, "at": now.isoformat(timespec="seconds")}
        if result:
            audit("reconcile.completed", **{k: int(v) for k, v in result.items()})
        return dict(result)

    def _interrupt_stale(self, visit_id: str, now: datetime) -> bool:
        """A visit left open by a crashed/stopped process: close it as INTERRUPTED at its LAST
        PERSISTED observation (we never invent a later departure time)."""
        from app.services.visit_manager import VisitSnapshot

        with self._factory() as db:
            v = db.get(Visit, visit_id)
            if v is None or v.state not in LIVE_STATES:
                return False
            snap = VisitSnapshot(
                id=v.id, visit_key=v.visit_key, session_id=v.session_id, source_id=v.source_id,
                source_label=v.source_label, visitor_id=v.visitor_id or "",
                visitor_number=v.visitor_number or 0, state="EXIT_PENDING",
                entry_video_ms=v.entry_video_ms, entry_wall=aware(v.entry_time),
                last_video_ms=v.last_observed_video_ms, last_wall=aware(v.last_observed_time),
                track_segments=v.track_segments, exit_basis="interrupted",
                closure_reason="api_restarted",
            )  # fmt: skip
        try:
            self.record(VisitAction("terminal", snap, "interrupted", None, None), now)
        except (EventConflict, OrderViolation):
            return False
        return True

    def erase_visitor(self, visitor_id: str) -> dict[str, int]:
        """Right to erasure: delete this visitor's evidence images and de-link their visits/events.
        Rows stay as an anonymous audit trail (times, source, outcome)."""
        files = 0
        with self._factory() as db:
            events = (
                db.execute(select(VisitEvent).where(VisitEvent.visitor_id == visitor_id))
                .scalars()
                .all()
            )
            for ev in events:
                if ev.image_path:
                    files += int(self.store.delete(ev.image_path))
                self._image_retry.pop(ev.id, None)
                ev.image_path, ev.image_sha256 = None, None
                if ev.image_status != "UNAVAILABLE":
                    ev.image_status = "ERASED"
                ev.visitor_id, ev.visitor_number = None, None
            db.execute(
                update(Visit)
                .where(Visit.visitor_id == visitor_id)
                .values(visitor_id=None, visitor_number=None)
            )
            db.commit()
        audit(
            "visitor.erased",
            visitor_id=visitor_id,
            evidence_files_deleted=files,
            events=len(events),
        )
        return {"events": len(events), "files": files}

    # ================================================================== health
    def health(self) -> dict[str, Any]:
        with self._factory() as db:
            by_status = {
                str(k): int(n)
                for k, n in db.execute(
                    select(VisitEvent.persistence_status, func.count()).group_by(
                        VisitEvent.persistence_status
                    )
                )
            }
            by_image = {
                str(k): int(n)
                for k, n in db.execute(
                    select(VisitEvent.image_status, func.count()).group_by(VisitEvent.image_status)
                )
            }
            open_visits = int(
                db.execute(
                    select(func.count()).select_from(Visit).where(Visit.state.in_(LIVE_STATES))
                ).scalar_one()
            )
        return {
            "events_by_persistence": by_status,
            "events_by_image_status": by_image,
            "open_visits": open_visits,
            "pending_image_retries": len(self._image_retry),
            "counters": dict(self.stats),
            "latency": {k: t.summary() for k, t in self.timing.items()},
            "last_reconcile": self.last_reconcile,
        }


__all__ = [
    "EventConflict",
    "EventRecorder",
    "EventsLogError",
    "OrderViolation",
    "RecordResult",
    "aware",
]
