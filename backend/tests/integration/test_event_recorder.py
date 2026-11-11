"""Durable event creation against a real PostgreSQL database, real files and a real events.log.

These tests are about the persistence guarantees: idempotent logical events, unique constraints,
recoverable side effects (image, log line), reconciliation after simulated crashes, erasure.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.core.config import VisitsConfig
from app.core.events import EventsLogError, EventsLogWriter
from app.core.logging import _reset, setup_logging
from app.models import ProcessingSession, Visit, VisitEvent, Visitor
from app.models.visit import EventDeliveryOutbox
from app.services.event_audit import verify_events_log
from app.services.event_recorder import EventConflict, EventRecorder, OrderViolation
from app.services.evidence_store import EvidenceStore, EvidenceWriteError
from app.services.image_store import ImageStoreError
from app.services.visit_manager import Crop, VisitAction, VisitSnapshot, event_id_for, visit_id_for
from tests.integration.test_database import alembic_cfg

pytestmark = pytest.mark.integration
SESSION = "5" * 32
VISITOR = "a" * 32
T0 = datetime(2026, 3, 4, 10, 0, tzinfo=UTC)


def jpeg(seed: int = 0, size: int = 96) -> bytes:
    rng = np.random.default_rng(seed)
    img = rng.integers(40, 220, size=(size, size, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


def black_jpeg() -> bytes:
    ok, buf = cv2.imencode(".jpg", np.zeros((96, 96, 3), dtype=np.uint8))
    assert ok
    return bytes(buf.tobytes())


class Rig:
    def __init__(self, engine: Engine, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.engine = engine
        self.factory = sessionmaker(engine, expire_on_commit=False)
        self.log_path = tmp_path / "logs" / "events.log"
        setup_logging("WARNING", tmp_path / "logs", self.log_path)
        self.store = EvidenceStore(tmp_path / "evidence")
        self.cfg = VisitsConfig(orphan_grace_seconds=0, max_attempts=3, retry_base_seconds=0.01)
        self.rec = EventRecorder(self.factory, self.store, self.cfg, EventsLogWriter())
        with self.factory() as db:
            db.add(
                ProcessingSession(
                    id=SESSION, source_id="clip", source_label="Clip", source_type="file",
                    state="RUNNING", frames_read=0, frames_processed=0, frames_skipped=0,
                    frames_dropped=0, corrupt_frames=0, detections_total=0, max_faces_in_frame=0,
                )
            )  # fmt: skip
            db.add(
                Visitor(
                    id=VISITOR, visitor_number=7, registration_key="k7", status="REGISTERED",
                    model_version="m", recognition_count=0,
                )
            )  # fmt: skip
            db.commit()

    def new_recorder(self) -> EventRecorder:
        """A 'restarted process': same database and files, no in-memory state."""
        return EventRecorder(self.factory, self.store, self.cfg, EventsLogWriter())

    def snap(self, ordinal: int = 1, **kw: Any) -> VisitSnapshot:
        key = f"{SESSION}:{VISITOR}:{ordinal}"
        base: dict[str, Any] = {
            "id": visit_id_for(key), "visit_key": key, "session_id": SESSION, "source_id": "clip",
            "source_label": "Clip", "visitor_id": VISITOR, "visitor_number": 7,
            "state": "ACTIVE", "entry_video_ms": 1000, "entry_wall": T0,
            "last_video_ms": 9000, "last_wall": T0 + timedelta(seconds=8), "track_segments": 1,
        }  # fmt: skip
        return VisitSnapshot(**{**base, **kw})

    def entry(self, ordinal: int = 1, crop: bytes | None = None, **kw: Any) -> VisitAction:
        data = jpeg(1) if crop is None else crop
        return VisitAction(
            "entry", self.snap(ordinal, **kw), "entry", Crop(data, 0.8, 1000), "observation"
        )

    def exit(
        self, ordinal: int = 1, crop: bytes | None = b"", etype: str = "exit", **kw: Any
    ) -> VisitAction:
        data = jpeg(2) if crop == b"" else crop
        snap = self.snap(
            ordinal,
            **{
                "state": "EXIT_PENDING",
                "exit_basis": "absence",
                "closure_reason": "absence_timeout",
                **kw,
            },
        )
        return VisitAction(
            "terminal", snap, etype,  # type: ignore[arg-type]
            None if data is None else Crop(data, 0.7, 9000),
            None if data is None else "last_good_observation",
        )  # fmt: skip

    def log_lines(self) -> list[dict[str, Any]]:
        for h in logging.getLogger("katomaran.events").handlers:
            h.flush()
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if str(rec.get("message", "")).startswith("visit."):
                out.append(rec)
        return out

    def count(self, model: Any) -> int:
        with self.factory() as db:
            return int(db.execute(select(func.count()).select_from(model)).scalar_one())

    def event(self, event_id: str) -> VisitEvent:
        with self.factory() as db:
            ev = db.get(VisitEvent, event_id)
            assert ev is not None
            return ev


@pytest.fixture
def rig(isolated_schema: tuple[Any, Engine], tmp_path: Path) -> Iterator[Rig]:
    _, engine = isolated_schema
    command.upgrade(alembic_cfg(engine), "head")
    r = Rig(engine, tmp_path)
    yield r
    _reset(logging.getLogger())
    _reset(logging.getLogger("katomaran.events"))


# ===================================================================== exactly-once logical events
def test_entry_creates_visit_event_outbox_image_and_one_log_line(rig: Rig) -> None:
    res = rig.rec.record(rig.entry())
    assert res.created and res.delivered and res.image_status == "STORED"
    ev = rig.event(res.event_id)
    assert ev.event_type == "entry" and ev.persistence_status == "DELIVERED"
    assert ev.image_path == f"entries/2026-03-04/{ev.id}.jpg"
    assert (rig.store.root / ev.image_path).is_file()
    with rig.factory() as db:
        visit = db.get(Visit, ev.visit_id)
        assert visit is not None and visit.state == "ACTIVE" and visit.exit_time is None
        assert visit.visitor_id == VISITOR and visit.id != VISITOR  # visit id != visitor id
    lines = rig.log_lines()
    assert len(lines) == 1 and lines[0]["message"] == "visit.entry"
    data = lines[0]["data"]
    assert data["event_id"] == ev.id and data["visit_id"] == ev.visit_id
    assert data["visitor_label"] == "V00007" and data["delivery_attempt"] == 1
    assert data["image_ref"] == ev.image_path and data["persistence"] == "committed"
    assert "embedding" not in json.dumps(lines[0])


def test_repeating_the_same_event_creates_nothing_new(rig: Rig) -> None:
    first = rig.rec.record(rig.entry())
    again = rig.rec.record(rig.entry())
    assert first.event_id == again.event_id and not again.created and again.duplicate
    assert (
        rig.count(Visit) == 1 and rig.count(VisitEvent) == 1 and rig.count(EventDeliveryOutbox) == 1
    )
    assert len(rig.log_lines()) == 1
    assert len(rig.store.all_files()) == 1
    assert rig.rec.stats["duplicates_ignored"] == 1


def test_full_lifecycle_has_one_entry_and_one_exit(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    res = rig.rec.record(rig.exit())
    assert res.created and res.event_type == "exit"
    rig.rec.record(rig.exit())  # duplicate exit
    with rig.factory() as db:
        visit = db.execute(select(Visit)).scalar_one()
        assert visit.state == "CLOSED" and visit.exit_basis == "absence"
        assert visit.exit_time == T0 + timedelta(seconds=8)
        assert visit.exit_confirmed_at is not None and visit.exit_confirmed_at >= visit.exit_time
        types = sorted(db.execute(select(VisitEvent.event_type)).scalars())
    assert types == ["entry", "exit"]
    assert [m["message"] for m in rig.log_lines()] == ["visit.entry", "visit.exit"]


def test_exit_without_entry_is_refused_and_leaves_no_trace(rig: Rig) -> None:
    with pytest.raises(OrderViolation):
        rig.rec.record(rig.exit())
    assert rig.count(Visit) == 0 and rig.count(VisitEvent) == 0 and rig.log_lines() == []


def test_conflicting_duplicate_is_reported_not_accepted(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    other = rig.entry(entry_wall=T0 + timedelta(minutes=5))  # same visit, different entry time
    with pytest.raises(EventConflict):
        rig.rec.record(other)
    assert rig.rec.stats["conflicts"] == 1 and rig.count(VisitEvent) == 1


def test_interrupted_terminal_event_is_a_different_type_in_the_same_slot(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    snap_kw: dict[str, Any] = {"exit_basis": "interrupted", "closure_reason": "worker_lost"}
    action = rig.exit(etype="interrupted", **snap_kw)
    rig.rec.record(action)
    with rig.factory() as db:
        visit = db.execute(select(Visit)).scalar_one()
        assert visit.state == "INTERRUPTED" and visit.closure_reason == "worker_lost"
    with pytest.raises(
        EventConflict
    ):  # a later "exit" for the same visit: conflict, never a 2nd event
        rig.rec.record(rig.exit())
    assert rig.count(VisitEvent) == 2


# ===================================================================== database invariants
def test_database_rejects_a_second_entry_or_terminal_event_per_visit(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    rig.rec.record(rig.exit())
    with rig.factory() as db:
        ev = db.execute(select(VisitEvent).where(VisitEvent.slot == "entry")).scalar_one()
        for slot, etype in (("entry", "entry"), ("terminal", "exit")):
            db.add(
                VisitEvent(
                    id=uuid.uuid4().hex, visit_id=ev.visit_id, visitor_id=VISITOR, visitor_number=7,
                    event_type=etype, slot=slot, idempotency_key=f"other-{slot}",
                    payload_hash="x", event_time=T0, video_ms=0, image_status="UNAVAILABLE",
                    persistence_status="PENDING",
                )
            )  # fmt: skip
            with pytest.raises(IntegrityError):
                db.commit()
            db.rollback()


def test_database_rejects_inconsistent_rows(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    base = (
        "INSERT INTO visit_events (id, visit_id, event_type, slot, idempotency_key, payload_hash, "
        "event_time, video_ms, image_status, persistence_status) VALUES "
        "(:id, :v, :t, :s, :k, 'h', now(), 0, :i, 'PENDING')"
    )
    vid = visit_id_for(f"{SESSION}:{VISITOR}:1")
    bad: list[dict[str, Any]] = [
        {"t": "teleport", "s": "terminal", "i": "STORED"},  # unknown type
        {"t": "exit", "s": "entry", "i": "STORED"},  # slot/type mismatch
        {"t": "exit", "s": "terminal", "i": "FAKE"},  # unknown image status
    ]
    for n, params in enumerate(bad):
        with pytest.raises(IntegrityError), rig.engine.begin() as conn:
            conn.execute(text(base), {"id": uuid.uuid4().hex, "v": vid, "k": f"bad{n}", **params})
    with pytest.raises(IntegrityError), rig.engine.begin() as conn:  # exit before entry
        conn.execute(
            text("UPDATE visits SET exit_time = entry_time - interval '1 second', state = 'CLOSED'")
        )
    with pytest.raises(IntegrityError), rig.engine.begin() as conn:  # unknown state
        conn.execute(text("UPDATE visits SET state = 'GONE'"))


def test_failed_transaction_rolls_back_visit_and_event_together(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.rec._commit

    def broken(*a: Any, **k: Any) -> bool:
        try:
            return original(*a, **k)
        finally:
            raise RuntimeError("connection lost")

    # The commit itself succeeds in `original`; to test a rollback make the FIRST insert fail instead.
    monkeypatch.setattr(
        rig.rec, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    with pytest.raises(RuntimeError):
        rig.rec.record(rig.entry())
    monkeypatch.undo()
    assert (
        rig.count(Visit) == 0 and rig.count(VisitEvent) == 0 and rig.count(EventDeliveryOutbox) == 0
    )
    del broken


def test_event_is_not_created_when_visit_row_is_missing_for_a_foreign_key(rig: Rig) -> None:
    with rig.factory() as db:
        db.add(
            VisitEvent(
                id=uuid.uuid4().hex, visit_id="f" * 32, event_type="entry", slot="entry",
                idempotency_key="k", payload_hash="h", event_time=T0, video_ms=0,
                image_status="UNAVAILABLE", persistence_status="PENDING",
            )
        )  # fmt: skip
        with pytest.raises(IntegrityError):
            db.commit()


# ===================================================================== crash windows
def test_crash_after_image_before_database_leaves_an_orphan_that_a_retry_adopts(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = rig.entry()
    original = rig.rec._commit
    monkeypatch.setattr(
        rig.rec, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        rig.rec.record(action)
    assert rig.count(VisitEvent) == 0 and len(rig.store.all_files()) == 1  # orphan on disk
    monkeypatch.setattr(rig.rec, "_commit", original)
    res = rig.rec.record(action)  # the retry
    assert res.created and res.image_status == "STORED"
    assert len(rig.store.all_files()) == 1  # same file, no duplicate image
    assert rig.rec.reconcile(live_sessions={SESSION}).get("orphan_files_removed", 0) == 0


def test_orphan_files_are_removed_by_reconciliation_but_referenced_ones_are_not(rig: Rig) -> None:
    res = rig.rec.record(rig.entry())
    stray = rig.store.root / "exits" / "2026-03-04" / f"{'c' * 32}.jpg"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(jpeg(5))
    partial = stray.with_name("d" * 32 + ".jpg.part")
    partial.write_bytes(b"half")
    old = time.time() - 3600
    os.utime(stray, (old, old))
    os.utime(partial, (old, old))
    report = rig.rec.reconcile(live_sessions={SESSION})
    assert report["orphan_files_removed"] == 2
    assert not stray.exists() and not partial.exists()
    assert rig.store.exists(rig.event(res.event_id).image_path or "")


def test_log_failure_keeps_the_event_pending_and_retries_deliver_exactly_one_line(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_write = rig.rec._writer.write

    def failing(*a: Any, **k: Any) -> None:
        raise EventsLogError("disk full")

    monkeypatch.setattr(rig.rec._writer, "write", failing)
    res = rig.rec.record(rig.entry())
    assert res.created and not res.delivered
    ev = rig.event(res.event_id)
    assert ev.persistence_status == "PENDING"  # committed, not yet in the log
    with rig.factory() as db:
        ob = db.execute(select(EventDeliveryOutbox)).scalar_one()
        assert ob.status == "PENDING" and ob.attempts == 1 and ob.last_error == "EventsLogError"
    assert rig.log_lines() == []
    monkeypatch.setattr(rig.rec._writer, "write", real_write)
    assert rig.rec.deliver_due(now=datetime.now(UTC) + timedelta(minutes=1)) == 1
    lines = rig.log_lines()
    assert len(lines) == 1 and lines[0]["data"]["delivery_attempt"] == 2
    assert rig.event(res.event_id).persistence_status == "DELIVERED"
    assert rig.rec.deliver_due(now=datetime.now(UTC) + timedelta(minutes=1)) == 0  # nothing more


def test_persistent_log_failure_ends_dead_and_is_reported_not_silently_dropped(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rig.rec._writer, "write", lambda *a, **k: (_ for _ in ()).throw(EventsLogError("x"))
    )
    res = rig.rec.record(rig.entry())
    for hour in range(1, 6):  # each retry becomes due only after its back-off
        rig.rec.deliver_due(now=datetime.now(UTC) + timedelta(hours=hour))
    assert rig.event(res.event_id).persistence_status == "DEAD"
    assert rig.rec.health()["events_by_persistence"].get("DEAD") == 1
    assert rig.rec.stats["outbox_dead"] == 1


def test_crash_between_log_write_and_acknowledgement_is_an_explained_redelivery(rig: Rig) -> None:
    res = rig.rec.record(rig.entry())
    # Simulate: line written, process died before the outbox row was marked DELIVERED.
    with rig.factory() as db:
        ob = db.execute(select(EventDeliveryOutbox)).scalar_one()
        ob.status, ob.delivered_at = "PENDING", None
        ev = db.get(VisitEvent, res.event_id)
        assert ev is not None
        ev.persistence_status = "PENDING"
        db.commit()
    fresh = rig.new_recorder()
    assert fresh.reconcile(live_sessions={SESSION}, startup=True)["log_lines_delivered"] == 1
    lines = rig.log_lines()
    assert [ln["data"]["delivery_attempt"] for ln in lines] == [1, 2]
    report = verify_events_log(rig.factory, rig.log_path)
    assert report["redelivered_count"] == 1 and report["unexplained_duplicates_count"] == 0
    assert report["consistent"] and report["missing_in_log_count"] == 0


def test_storage_outage_marks_evidence_pending_then_stored_when_it_returns(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rig.store.save

    def broken(*a: Any, **k: Any) -> str:
        raise EvidenceWriteError("disk unavailable")

    monkeypatch.setattr(rig.store, "save", broken)
    res = rig.rec.record(rig.entry())
    assert res.created and res.image_status == "PENDING" and res.delivered
    assert rig.rec.health()["pending_image_retries"] == 1
    monkeypatch.setattr(rig.store, "save", real)
    assert rig.rec.retry_images() == 1
    ev = rig.event(res.event_id)
    assert ev.image_status == "STORED" and ev.image_sha256
    assert (rig.store.root / (ev.image_path or "")).is_file()


def test_evidence_lost_in_a_restart_is_reported_missing_never_fabricated(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rig.store, "save", lambda *a, **k: (_ for _ in ()).throw(EvidenceWriteError("no disk"))
    )
    res = rig.rec.record(rig.entry())
    monkeypatch.undo()
    restarted = rig.new_recorder()  # the in-memory bytes are gone
    assert restarted.reconcile(live_sessions={SESSION})["evidence_missing"] == 1
    ev = rig.event(res.event_id)
    assert ev.image_status == "MISSING"
    assert not any(rig.store.root.rglob("*.jpg"))  # nothing invented


def test_deleted_evidence_file_is_detected(rig: Rig) -> None:
    res = rig.rec.record(rig.entry())
    (rig.store.root / (rig.event(res.event_id).image_path or "")).unlink()
    assert rig.rec.reconcile(live_sessions={SESSION})["evidence_missing"] == 1
    assert rig.event(res.event_id).image_status == "MISSING"


@pytest.mark.parametrize("bad", ["black", "garbage", "tiny"])
def test_unusable_crops_are_never_stored_as_evidence(rig: Rig, bad: str) -> None:
    data = {"black": black_jpeg(), "garbage": b"\xff\xd8not a jpeg", "tiny": jpeg(3, size=20)}[bad]
    res = rig.rec.record(rig.entry(crop=data))
    assert res.created and res.image_status == "UNAVAILABLE"
    ev = rig.event(res.event_id)
    assert ev.image_path is None and ev.image_sha256 is None
    assert rig.store.all_files() == []
    assert rig.rec.stats["evidence_rejected"] == 1


def test_open_visit_after_a_crash_is_interrupted_at_its_last_persisted_observation(
    rig: Rig,
) -> None:
    rig.rec.record(rig.entry())
    rig.rec.touch(visit_id_for(f"{SESSION}:{VISITOR}:1"), T0 + timedelta(seconds=40), 41_000, 2)
    restarted = rig.new_recorder()
    report = restarted.reconcile(live_sessions=set())
    assert report["visits_interrupted"] == 1
    with rig.factory() as db:
        visit = db.execute(select(Visit)).scalar_one()
        assert visit.state == "INTERRUPTED" and visit.exit_basis == "interrupted"
        assert visit.closure_reason == "api_restarted"
        assert visit.exit_time == T0 + timedelta(seconds=40)  # not "now": no invented departure
        terminal = db.execute(select(VisitEvent).where(VisitEvent.slot == "terminal")).scalar_one()
        assert terminal.event_type == "interrupted"
        # No new face was captured: the entry image is used and labelled as such.
        assert terminal.image_kind == "entry_representative" and terminal.image_status == "STORED"
        assert terminal.image_path and terminal.image_path.startswith("exits/")
    assert restarted.reconcile(live_sessions=set()).get("visits_interrupted", 0) == 0
    assert rig.count(VisitEvent) == 2


def test_reconcile_leaves_visits_of_a_live_session_alone(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    assert rig.rec.reconcile(live_sessions={SESSION}).get("visits_interrupted", 0) == 0
    with rig.factory() as db:
        assert db.execute(select(Visit.state)).scalar_one() == "ACTIVE"


def test_touch_is_monotonic_and_ignores_closed_visits(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    vid = visit_id_for(f"{SESSION}:{VISITOR}:1")
    rig.rec.touch(vid, T0 + timedelta(seconds=30), 30_000, 1)
    rig.rec.touch(vid, T0 + timedelta(seconds=10), 10_000, 1)  # stale
    with rig.factory() as db:
        assert db.get(Visit, vid).last_observed_video_ms == 30_000  # type: ignore[union-attr]
    rig.rec.record(rig.exit())
    rig.rec.touch(vid, T0 + timedelta(seconds=99), 99_000, 1)
    with rig.factory() as db:
        assert db.get(Visit, vid).last_observed_video_ms == 9000  # type: ignore[union-attr]


# ===================================================================== concurrency
def test_concurrent_identical_deliveries_create_one_event_and_one_log_line(rig: Rig) -> None:
    action = rig.entry()
    results: list[Any] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def run() -> None:
        barrier.wait()
        try:
            results.append(rig.rec.record(action))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert (
        rig.count(Visit) == 1 and rig.count(VisitEvent) == 1 and rig.count(EventDeliveryOutbox) == 1
    )
    assert sum(1 for r in results if r.created) == 1
    assert len(rig.store.all_files()) == 1
    report = verify_events_log(rig.factory, rig.log_path)
    assert (
        report["unexplained_duplicates_count"] == 0
    )  # attempts are numbered, so any repeat is explained


def test_two_recorders_on_one_database_do_not_duplicate(rig: Rig) -> None:
    other = rig.new_recorder()
    a, b = rig.rec.record(rig.entry()), other.record(rig.entry())
    assert a.event_id == b.event_id and rig.count(VisitEvent) == 1


# ===================================================================== erasure
def test_erasing_a_visitor_deletes_evidence_and_delinks_the_audit_rows(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    rig.rec.record(rig.exit())
    paths = [
        e.image_path
        for e in [
            rig.event(event_id_for(visit_id_for(f"{SESSION}:{VISITOR}:1"), s))
            for s in ("entry", "terminal")
        ]
    ]
    assert all(p and (rig.store.root / p).is_file() for p in paths)
    summary = rig.rec.erase_visitor(VISITOR)
    assert summary == {"events": 2, "files": 2}
    assert rig.store.all_files() == []
    with rig.factory() as db:
        db.delete(db.get(Visitor, VISITOR))  # the identity itself goes too
        db.commit()
        events = db.execute(select(VisitEvent)).scalars().all()
        assert len(events) == 2 and all(
            e.visitor_id is None
            and e.visitor_number is None
            and e.image_path is None
            and e.image_status == "ERASED"
            for e in events
        )
        visit = db.execute(select(Visit)).scalar_one()
        assert visit.visitor_id is None and visit.visitor_number is None and visit.state == "CLOSED"


def test_reconcile_completes_an_interrupted_erasure(rig: Rig) -> None:
    res = rig.rec.record(rig.entry())
    path = rig.event(res.event_id).image_path or ""
    with rig.factory() as db:  # the visitor row was deleted (FK -> NULL) but the files survived
        db.delete(db.get(Visitor, VISITOR))
        db.commit()
    assert (rig.store.root / path).is_file()
    assert rig.rec.reconcile(live_sessions={SESSION})["erasure_completed"] == 1
    assert not (rig.store.root / path).exists()
    assert rig.event(res.event_id).image_status == "ERASED"


# ===================================================================== log verification
def test_log_verification_detects_missing_and_duplicated_lines(rig: Rig) -> None:
    a = rig.rec.record(rig.entry(1))
    b = rig.rec.record(rig.entry(2))
    clean = verify_events_log(rig.factory, rig.log_path)
    assert clean["consistent"] and clean["database_events"] == 2 and clean["log_lines"] == 2

    lines = rig.log_path.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if a.event_id not in ln]  # lose event a's line
    kept.append(next(ln for ln in lines if b.event_id in ln))  # duplicate b's line verbatim
    kept.append("{ this is not json")
    rig.log_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    report = verify_events_log(rig.factory, rig.log_path)
    assert report["missing_in_log"] == [a.event_id] and not report["consistent"]
    assert report["unexplained_duplicates"] == [b.event_id]
    assert report["malformed_lines"] == 1


def test_log_verification_flags_events_missing_from_the_database(rig: Rig) -> None:
    rig.rec.record(rig.entry())
    rig.engine.dispose()
    with rig.factory() as db:
        db.execute(text("DELETE FROM visit_events"))
        db.commit()
    assert verify_events_log(rig.factory, rig.log_path)["logged_not_in_db_count"] == 1


# ===================================================================== evidence store safety
@pytest.mark.parametrize(
    "ref",
    [
        "../x.jpg",
        "entries/../../x.jpg",
        "entries/2026-03-04/../../../x.jpg",
        "entries/2026-03-04/" + "a" * 31 + ".jpg",
        "C:/Windows/win.ini",
        "/etc/passwd",
        "entries/2026-03-04/" + "a" * 32 + ".png",
    ],
)
def test_evidence_references_cannot_escape_the_store(rig: Rig, ref: str) -> None:
    with pytest.raises(ImageStoreError):
        rig.store.read(ref)
    assert rig.store.exists(ref) is False
    with pytest.raises(ImageStoreError):
        rig.store.save(ref, jpeg())


def test_evidence_write_is_atomic_and_idempotent(rig: Rig) -> None:
    rel = "entries/2026-03-04/" + "e" * 32 + ".jpg"
    sha = rig.store.save(rel, jpeg(7))
    assert rig.store.save(rel, jpeg(8)) == sha  # an event's evidence never changes
    assert not list(rig.store.root.rglob("*.part"))
    assert rig.store.read(rel) == jpeg(7)


def test_erasure_during_an_open_visit_records_the_exit_without_identity_or_evidence(
    rig: Rig,
) -> None:
    rig.rec.record(rig.entry())
    rig.rec.erase_visitor(VISITOR)
    with rig.factory() as db:
        db.delete(db.get(Visitor, VISITOR))  # the identity is gone while the visit is still open
        db.commit()
    res = rig.rec.record(rig.exit())  # the lifecycle later closes the visit by absence
    assert res.created and res.image_status == "ERASED"
    ev = rig.event(res.event_id)
    assert ev.visitor_id is None and ev.visitor_number is None and ev.image_path is None
    assert ev.image_status == "ERASED"
    assert rig.store.all_files() == []  # nothing biometric was written after the erasure
    with rig.factory() as db:
        visit = db.execute(select(Visit)).scalar_one()
        assert visit.state == "CLOSED" and visit.visitor_id is None
    assert rig.rec.stats["events_for_erased_visitors"] == 1


def test_an_entry_for_an_already_erased_visitor_is_recorded_anonymously(rig: Rig) -> None:
    with rig.factory() as db:
        db.delete(db.get(Visitor, VISITOR))
        db.commit()
    res = rig.rec.record(rig.entry())
    assert res.created
    ev = rig.event(res.event_id)
    assert ev.visitor_id is None and ev.image_status == "ERASED" and rig.store.all_files() == []
