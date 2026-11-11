"""Analytics aggregation on a real PostgreSQL database: counting semantics, scopes, reconciliation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.models import (
    FaceEmbedding,
    IdentityAssociation,
    ProcessingSession,
    Visit,
    VisitEvent,
    Visitor,
)
from app.services.analytics import AnalyticsService, Scope
from tests.integration.test_database import alembic_cfg

pytestmark = pytest.mark.integration
T0 = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)


class World:
    """A tiny hand-built database whose correct answers are known in advance."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.factory = sessionmaker(engine, expire_on_commit=False)
        self.live: set[str] = set()
        self.service = AnalyticsService(self.factory, lambda: set(self.live))
        self._n = 0

    def session(
        self,
        sid: str,
        source: str = "hall",
        created: datetime = T0,
        state: str = "COMPLETED",
        summary: dict[str, Any] | None = None,
    ) -> str:
        with self.factory() as db:
            db.add(
                ProcessingSession(
                    id=sid, source_id=source, source_label=source.title(), source_type="file", state=state,
                    frames_read=0, frames_processed=0, frames_skipped=0, frames_dropped=0, corrupt_frames=0,
                    detections_total=0, max_faces_in_frame=0, created_at=created, summary=summary,
                    active_slot=1 if state in ("RUNNING", "STARTING") else None,
                )
            )  # fmt: skip
            db.commit()
        return sid

    def visitor(self, number: int, first_session: str | None, with_embedding: bool = True) -> str:
        vid = f"{number:032x}"
        with self.factory() as db:
            db.add(
                Visitor(
                    id=vid, visitor_number=number, registration_key=f"k{number}", status="REGISTERED",
                    model_version="m", first_session_id=first_session, recognition_count=0,
                )
            )  # fmt: skip
            db.flush()
            if with_embedding:
                db.add(
                    FaceEmbedding(
                        id=uuid.uuid4().hex, visitor_id=vid, model_version="m", dim=1, vector=b"\x00" * 4,
                        vector_sha256=uuid.uuid4().hex + uuid.uuid4().hex, quality_score=0.9,
                    )
                )  # fmt: skip
            db.commit()
        return vid

    def visit(
        self, visitor: str | None, session: str, entry: datetime, *, source: str = "hall",
        end: str | None = "absence", seconds: int = 30, state: str | None = None,
    ) -> str:  # fmt: skip
        """``end``: absence | observation_ended | interrupted | None (still open)."""
        self._n += 1
        vid = f"{0xC0000000 + self._n:032x}"
        closed = end is not None
        final = state or (
            ("INTERRUPTED" if end == "interrupted" else "CLOSED") if closed else "ACTIVE"
        )
        exit_t = entry + timedelta(seconds=seconds)
        number = None
        if visitor:
            with self.factory() as db:
                number = db.get(Visitor, visitor).visitor_number  # type: ignore[union-attr]
        with self.factory() as db:
            db.add(
                Visit(
                    id=vid, visit_key=f"k{self._n}", visitor_id=visitor, visitor_number=number, session_id=session,
                    source_id=source, source_label=source.title(), state=final, entry_time=entry, entry_video_ms=0,
                    last_observed_time=exit_t if closed else entry, last_observed_video_ms=0,
                    exit_time=exit_t if closed else None, exit_video_ms=seconds * 1000 if closed else None,
                    exit_basis=end, closure_reason="x" if closed else None, track_segments=1,
                )
            )  # fmt: skip
            db.flush()
            db.add(
                VisitEvent(
                    id=f"{0xE0000000 + self._n:032x}", visit_id=vid, visitor_id=visitor, visitor_number=number,
                    event_type="entry", slot="entry", idempotency_key=f"{vid}:entry", payload_hash="h",
                    event_time=entry, video_ms=0, image_status="STORED", persistence_status="DELIVERED",
                )
            )  # fmt: skip
            db.flush()
            if closed:
                db.add(
                    VisitEvent(
                        id=f"{0xF0000000 + self._n:032x}", visit_id=vid, visitor_id=visitor, visitor_number=number,
                        event_type="interrupted" if end == "interrupted" else "exit", slot="terminal",
                        idempotency_key=f"{vid}:terminal", payload_hash="h", event_time=exit_t, video_ms=seconds * 1000,
                        image_status="STORED", persistence_status="DELIVERED",
                    )
                )  # fmt: skip
            db.commit()
        return vid

    def association(self, session: str, track: int, state: str, visitor: str | None = None) -> None:
        with self.factory() as db:
            db.add(
                IdentityAssociation(
                    id=uuid.uuid4().hex, session_id=session, track_id=track, visitor_id=visitor, state=state,
                    observations=3, conflicts=0, registered_here=0,
                )
            )  # fmt: skip
            db.commit()

    def overview(self, **scope: Any) -> dict[str, Any]:
        return self.service.overview(Scope(**scope))


@pytest.fixture
def w(isolated_schema: tuple[Any, Engine], tmp_path: Path) -> Iterator[World]:
    _, engine = isolated_schema
    command.upgrade(alembic_cfg(engine), "head")
    yield World(engine)


S1, S2, S3 = "1" * 32, "2" * 32, "3" * 32


def test_an_empty_database_is_all_zeros_and_consistent(w: World) -> None:
    o = w.overview()
    for key in (
        "registered_visitors",
        "unique_visitors_in_scope",
        "visits",
        "entry_events",
        "terminal_events",
        "active_visits",
        "unresolved_tracks",
    ):
        assert o[key] == 0, key
    assert o["last_event_recorded_at"] is None
    assert w.service.timeline(Scope(), "day")["points"] == []
    assert (
        w.service.sources(Scope())["items"] == []
        and w.service.sessions(Scope(), 10, 0)["total"] == 0
    )
    assert w.service.coverage(Scope())["resolved_share_of_evaluated_tracks"] is None
    assert w.service.reconcile()["consistent"] is True


def test_one_visitor_with_many_tracks_and_visits_is_one_unique_visitor(w: World) -> None:
    w.session(S1)
    a = w.visitor(1, S1)
    for t in range(1, 6):  # five tracks of the same person
        w.association(S1, t, "RECOGNIZED", a)
    w.visit(a, S1, T0 + timedelta(minutes=1))
    w.visit(
        a, S1, T0 + timedelta(minutes=10), end="observation_ended"
    )  # leaves and comes back in the same session
    o = w.overview()
    assert (o["registered_visitors"], o["unique_visitors_in_scope"]) == (1, 1)
    assert (o["visits"], o["entry_events"], o["terminal_events"]) == (2, 2, 2)  # visits != visitors
    assert o["terminal_by_basis"] == {"absence": 1, "observation_ended": 1, "interrupted": 0}
    assert (
        o["returning_visitors_in_scope"] == 0 and o["new_visitors_in_scope"] == 1
    )  # same session is not "returning"
    assert o["unresolved_tracks"] == 0


def test_returning_visitor_means_a_later_session_not_a_second_track_or_visit(w: World) -> None:
    w.session(S1, created=T0)
    w.session(S2, created=T0 + timedelta(days=1))
    a, b = w.visitor(1, S1), w.visitor(2, S2)
    w.visit(a, S1, T0 + timedelta(hours=1))
    w.visit(a, S2, T0 + timedelta(days=1, hours=1))  # a comes back in a later session
    w.visit(b, S2, T0 + timedelta(days=1, hours=2))  # b is new in S2
    everything = w.overview()
    assert everything["unique_visitors_in_scope"] == 2 and everything["registered_visitors"] == 2
    assert (
        everything["returning_visitors_in_scope"] == 1 and everything["new_visitors_in_scope"] == 1
    )
    assert everything["visits"] == 3
    s1 = w.overview(session_id=S1)
    assert (s1["unique_visitors_in_scope"], s1["returning_visitors_in_scope"]) == (1, 0)
    s2 = w.overview(session_id=S2)
    assert (
        s2["unique_visitors_in_scope"],
        s2["returning_visitors_in_scope"],
        s2["new_visitors_in_scope"],
    ) == (2, 1, 1)
    assert s2["registered_visitors"] == 2  # global value does not follow the filter


def test_simultaneous_visitors_and_interruptions_are_counted_honestly(w: World) -> None:
    w.session(S1)
    ids = [w.visitor(i, S1) for i in range(1, 4)]
    same = T0 + timedelta(minutes=5)
    w.visit(ids[0], S1, same)
    w.visit(ids[1], S1, same, end="interrupted")
    w.visit(ids[2], S1, same, end="observation_ended")
    o = w.overview()
    assert (o["unique_visitors_in_scope"], o["visits"]) == (3, 3)
    assert o["terminal_by_basis"] == {"absence": 1, "observation_ended": 1, "interrupted": 1}
    assert o["terminal_events"] == 3  # kept distinguishable, none called a departure


def test_active_visits_use_session_liveness_not_stale_rows(w: World) -> None:
    w.session(S1, state="RUNNING")
    w.session(S2)  # a crashed run that never reconciled its visit
    a, b = w.visitor(1, S1), w.visitor(2, S2)
    w.visit(a, S1, T0, end=None)
    w.visit(b, S2, T0, end=None)
    w.visit(a, S1, T0 + timedelta(minutes=1), end=None, state="TEMPORARILY_MISSING")
    w.live = {S1}
    o = w.overview()
    assert (o["active_visits"], o["active_visits_temporarily_missing"], o["stale_open_visits"]) == (
        2,
        1,
        1,
    )
    w.live = set()  # after the session ends nobody is present
    o = w.overview()
    assert (o["active_visits"], o["stale_open_visits"]) == (0, 3)
    assert w.overview(session_id=S2)["stale_open_visits"] == 1
    rec = w.service.reconcile()
    stale = next(c for c in rec["checks"] if c["name"].startswith("stale_open_visits"))
    assert stale["ok"] and stale["actual"] == 3 and rec["consistent"]


def test_erasure_policy_unique_counts_drop_but_visits_and_events_stay(w: World) -> None:
    w.session(S1)
    a, b = w.visitor(1, S1), w.visitor(2, S1)
    w.visit(a, S1, T0 + timedelta(minutes=1))
    w.visit(b, S1, T0 + timedelta(minutes=2))
    with w.factory() as db:  # what DELETE /visitors/{id} leaves behind (FK ON DELETE SET NULL)
        db.execute(
            text("UPDATE visit_events SET image_status='ERASED' WHERE visitor_id = :v"), {"v": a}
        )
        db.delete(db.get(Visitor, a))
        db.commit()
    o = w.overview()
    assert (o["registered_visitors"], o["unique_visitors_in_scope"]) == (1, 1)
    assert (o["visits"], o["entry_events"], o["terminal_events"], o["visits_without_identity"]) == (
        2,
        2,
        2,
        1,
    )
    assert w.service.reconcile()["consistent"] is True


def test_scope_boundaries_are_half_open_utc_and_timezone_safe(w: World) -> None:
    w.session(S1)
    a = w.visitor(1, S1)
    day2 = T0 + timedelta(days=1)
    w.visit(a, S1, day2 - timedelta(milliseconds=1))  # last instant of day 1
    w.visit(a, S1, day2)  # first instant of day 2
    assert w.overview(since=T0, until=day2)["visits"] == 1  # until is exclusive
    assert (
        w.overview(since=day2, until=day2 + timedelta(days=1))["visits"] == 1
    )  # since is inclusive
    assert w.overview(since=T0, until=day2 + timedelta(days=1))["visits"] == 2
    ist = timezone(timedelta(hours=5, minutes=30))
    local = day2.astimezone(ist)  # 05:30 local == 00:00 UTC: the same instant
    assert w.overview(since=local, until=local + timedelta(days=1))["visits"] == 1
    assert w.overview(since=day2 + timedelta(days=5))["visits"] == 0


def test_source_and_session_filters_apply_to_every_visit_number(w: World) -> None:
    w.session(S1, source="hall")
    w.session(S2, source="gate")
    a, b = w.visitor(1, S1), w.visitor(2, S2)
    w.visit(a, S1, T0, source="hall")
    w.visit(b, S2, T0, source="gate")
    w.visit(a, S2, T0 + timedelta(hours=1), source="gate", end="interrupted")
    gate = w.overview(source_id="gate")
    assert (
        gate["visits"],
        gate["unique_visitors_in_scope"],
        gate["entry_events"],
        gate["terminal_events"],
    ) == (2, 2, 2, 2)
    assert gate["terminal_by_basis"]["interrupted"] == 1
    assert w.overview(source_id="nowhere")["visits"] == 0
    assert w.overview(source_id="hall", session_id=S2)["visits"] == 0
    sources = {s["source_id"]: s for s in w.service.sources(Scope())["items"]}
    assert (
        sources["gate"]["visits"],
        sources["gate"]["unique_visitors"],
        sources["hall"]["visits"],
    ) == (2, 2, 1)


def test_timeline_matches_the_overview_and_uses_utc_buckets(w: World) -> None:
    w.session(S1)
    a, b = w.visitor(1, S1), w.visitor(2, S1)
    w.visit(a, S1, T0 + timedelta(hours=1, minutes=10))
    w.visit(a, S1, T0 + timedelta(hours=1, minutes=40), end="interrupted")
    w.visit(b, S1, T0 + timedelta(hours=4))
    tl = w.service.timeline(Scope(), "hour")
    starts = [p["bucket_start"] for p in tl["points"]]
    assert (
        starts[0] == "2026-03-01T01:00:00Z"
        and starts[-1] == "2026-03-01T04:00:00Z"
        and len(starts) == 4
    )  # gaps filled
    assert tl["timezone"] == "UTC" and not tl["truncated"]
    first = tl["points"][0]
    assert (first["entries"], first["visits"], first["unique_visitors"], first["interrupted"]) == (
        2,
        2,
        1,
        1,
    )
    o = w.overview()
    assert sum(p["entries"] for p in tl["points"]) == o["entry_events"]
    assert (
        sum(
            p["exits_absence"] + p["exits_observation_ended"] + p["interrupted"]
            for p in tl["points"]
        )
        == o["terminal_events"]
    )
    day = w.service.timeline(Scope(), "day")["points"]
    assert len(day) == 1 and day[0]["unique_visitors"] == 2 and day[0]["entries"] == 3


def test_sessions_breakdown_counts_registrations_and_interruptions(w: World) -> None:
    w.session(S1, created=T0)
    w.session(S2, created=T0 + timedelta(days=1), state="ERROR")
    a, b = w.visitor(1, S1), w.visitor(2, S2)
    w.visit(a, S1, T0)
    w.visit(a, S2, T0 + timedelta(days=1), end="interrupted")
    w.visit(b, S2, T0 + timedelta(days=1, hours=1))
    page = w.service.sessions(Scope(), 10, 0)
    assert page["total"] == 2 and [s["session_id"] for s in page["items"]] == [
        S2,
        S1,
    ]  # newest first
    s2 = page["items"][0]
    assert (
        s2["visits"],
        s2["unique_visitors"],
        s2["interrupted_visits"],
        s2["visitors_registered_here"],
    ) == (2, 2, 1, 1)
    assert w.service.sessions(Scope(), 1, 1)["items"][0]["session_id"] == S1  # stable paging


def test_coverage_is_a_track_diagnostic_never_extra_visitors(w: World) -> None:
    w.session(
        S1,
        summary={
            "tracks_confirmed_total": 10,
            "recog_processed": 40,
            "recog_usable": 12,
            "recog_dropped": 3,
            "recog_errors": 1,
        },
    )
    a = w.visitor(1, S1)
    w.visit(a, S1, T0)
    w.association(S1, 1, "REGISTERED", a)
    w.association(S1, 2, "RECOGNIZED", a)
    for t, st in enumerate(
        ("INSUFFICIENT_QUALITY", "INSUFFICIENT_QUALITY", "AMBIGUOUS", "MATCH_PENDING", "REJECTED"),
        start=3,
    ):
        w.association(S1, t, st)
    c = w.service.coverage(Scope())
    assert (
        c["confirmed_tracks"],
        c["tracks_with_recognition_observations"],
        c["tracks_resolved"],
        c["tracks_unresolved"],
    ) == (10, 7, 2, 5)
    assert c["unresolved_breakdown"] == {
        "low_quality": 2,
        "undecided": 1,
        "registration_pending": 0,
        "ambiguous": 1,
        "rejected": 1,
    }
    assert c["face_crops_rejected_by_quality_gate"] == 28 and c["recognition_errors"] == 1
    assert c["resolved_share_of_evaluated_tracks"] == round(2 / 7, 4)
    assert "not person-level recall" in c["disclaimer"]
    o = w.overview()
    assert (
        o["unique_visitors_in_scope"] == 1 and o["unresolved_tracks"] == 5
    )  # 5 unresolved tracks add nobody


def test_reconciliation_reports_corruption_without_repairing_it(w: World) -> None:
    w.session(S1)
    a = w.visitor(1, S1)
    vid = w.visit(a, S1, T0)
    before = w.overview()
    with w.factory() as db:  # simulate damage: a closed visit that lost its terminal event
        db.execute(
            text("DELETE FROM visit_events WHERE visit_id = :v AND slot = 'terminal'"), {"v": vid}
        )
        db.commit()
    rec = w.service.reconcile()
    assert (
        rec["consistent"] is False and rec["errors"] >= 1 and rec["mode"].startswith("report-only")
    )
    bad = next(c for c in rec["checks"] if c["name"].startswith("every_visit_has_one_entry"))
    assert not bad["ok"] and bad["actual"] == 1 and vid in bad["detail"]
    after = w.overview()
    assert (
        after["visits"] == before["visits"]
        and after["terminal_events"] == before["terminal_events"] - 1
    )
    with w.factory() as db:  # nothing was "fixed" behind our back
        assert db.execute(select(func.count()).select_from(VisitEvent)).scalar_one() == 1


def test_reconciliation_detects_a_type_basis_mismatch_and_missing_embeddings(w: World) -> None:
    w.session(S1)
    a = w.visitor(1, S1, with_embedding=False)
    vid = w.visit(a, S1, T0, end="interrupted")
    with w.factory() as db:
        db.execute(
            text("UPDATE visit_events SET event_type='exit' WHERE visit_id=:v AND slot='terminal'"),
            {"v": vid},
        )
        db.commit()
    rec = w.service.reconcile()
    names = {c["name"] for c in rec["checks"] if not c["ok"]}
    assert {"terminal_event_type_matches_closure_basis", "visitors_without_embeddings"} <= names
    assert rec["consistent"] is False and rec["warnings"] >= 1


def test_numbers_survive_a_restart_and_retried_events_do_not_inflate_them(
    w: World, tmp_path: Path
) -> None:
    import logging
    from datetime import datetime as dt

    from app.core.config import VisitsConfig
    from app.core.logging import _reset, setup_logging
    from app.services.event_recorder import EventRecorder
    from app.services.evidence_store import EvidenceStore
    from app.services.visit_manager import VisitAction, VisitSnapshot, visit_id_for

    setup_logging("WARNING", tmp_path / "logs", tmp_path / "logs" / "events.log")
    try:
        w.session(S1)
        a = w.visitor(1, S1)
        key = f"{S1}:{a}:1"
        snap = VisitSnapshot(
            id=visit_id_for(key), visit_key=key, session_id=S1, source_id="hall", source_label="Hall", visitor_id=a,
            visitor_number=1, state="ACTIVE", entry_video_ms=0, entry_wall=dt(2026, 3, 1, 1, tzinfo=UTC),
            last_video_ms=5000, last_wall=dt(2026, 3, 1, 1, 0, 5, tzinfo=UTC), track_segments=1,
        )  # fmt: skip
        rec = EventRecorder(w.factory, EvidenceStore(tmp_path / "ev"), VisitsConfig())
        for _ in range(3):  # the same entry delivered three times
            rec.record(VisitAction("entry", snap, "entry", None, None))
        assert w.overview()["entry_events"] == 1 and w.overview()["visits"] == 1
        restarted = AnalyticsService(w.factory, lambda: set())  # a new process
        assert restarted.overview(Scope())["unique_visitors_in_scope"] == 1
        assert restarted.reconcile()["errors"] == 0
    finally:
        _reset(logging.getLogger())
        _reset(logging.getLogger("katomaran.events"))


def test_aggregation_matches_an_independent_recount_on_a_random_world(w: World) -> None:
    import random

    rnd = random.Random(7)  # noqa: S311 - deterministic test data
    sessions = [
        w.session(
            f"{i:032x}",
            source=rnd.choice(["hall", "gate", "lab"]),
            created=T0 + timedelta(hours=i * 7),
        )
        for i in range(1, 7)
    ]
    people = [w.visitor(n, rnd.choice(sessions)) for n in range(1, 21)]
    rows = []
    for _ in range(120):
        sid = rnd.choice(sessions)
        who = rnd.choice(people)
        t = T0 + timedelta(minutes=rnd.randrange(0, 60 * 48))
        end = rnd.choice(["absence", "observation_ended", "interrupted"])
        w.visit(who, sid, t, end=end, source=rnd.choice(["hall", "gate", "lab"]))
        rows.append((who, sid, t))
    since, until = T0 + timedelta(hours=10), T0 + timedelta(hours=30)
    expected = {(who, sid) for who, sid, t in rows if since <= t < until}
    o = w.overview(since=since, until=until)
    assert o["visits"] == sum(1 for _w, _s, t in rows if since <= t < until)
    assert o["unique_visitors_in_scope"] == len({who for who, _s in expected})
    assert o["entry_events"] == o["visits"] and o["terminal_events"] == o["visits"]
    assert w.service.reconcile()["consistent"] is True
