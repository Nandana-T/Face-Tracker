"""Visitor analytics (Phase 5): database aggregation with explicit, documented definitions.

EVERYTHING here is a SQL aggregate over rows PostgreSQL already holds (visitors, visits, visit_events,
processing_sessions, identity_associations). No per-frame data, no second counter that could drift.

Vocabulary - these numbers are different things and are never added together:
  registered visitor   a persistent face identity (``visitors`` row). Counted EXACTLY, globally.
  unique visitors      COUNT(DISTINCT visits.visitor_id) over the visits in scope.
  visit                one presence episode of one visitor in one session (``visits`` row).
  entry event          the logical entry event of a visit (one per visit).
  terminal event       the logical end of a visit: ``exit`` (basis ``absence`` or ``observation_ended``)
                       or ``interrupted``. None of them is a verified physical departure.
  active visit         an open visit (ACTIVE / TEMPORARILY_MISSING / EXIT_PENDING) of a session that is
                       LIVE right now. Open rows of sessions that are not live (a crashed process, not yet
                       reconciled) are reported separately as ``stale_open_visits`` and never as present.
  track (diagnostic)   a temporary session-scoped trajectory with at least one recognition observation.
                       Tracks without a resolved identity are a COVERAGE diagnostic - never extra people.
None of these is a count of physical people: that needs identity ground truth (see docs/phase-5-report.md).

SCOPE. ``since``/``until`` select visits by ENTRY time, half-open ``[since, until)`` in UTC (so adjacent
ranges never double count); ``source_id`` / ``session_id`` narrow by the visit's source / session.
All visit-derived numbers (unique visitors, visits, entries, terminal events, returning, timeline,
sources) use the same scope; terminal events belong to the visit they end, wherever their own time
falls. Registered visitors, active/stale visits (a live "now" view, source/session filters only) and the
coverage diagnostics are documented separately in the response.

RETURNING. A visitor in scope is *returning* when at least one in-scope visit happened in a session
different from the session in which the visitor was first registered (``visitors.first_session_id``).
A second track, or a second visit inside the registering session, does not make a visitor returning.

ERASURE. Erasing a visitor deletes the ``visitors`` row; their visits/events stay de-identified
(``visitor_id`` NULL). So an erased visitor leaves ``registered`` and ``unique visitors`` (no biometric or
identity data is retained to preserve dashboard numbers) but their visits, entries and terminal events stay
counted, and ``visits_without_identity`` makes the difference visible.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, case, distinct, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models.processing_session import ACTIVE_STATES, ProcessingSession
from app.models.visit import EventDeliveryOutbox, Visit, VisitEvent
from app.models.visitor import FaceEmbedding, IdentityAssociation, Visitor

OPEN_STATES = ("ACTIVE", "TEMPORARILY_MISSING", "EXIT_PENDING")
RESOLVED = ("RECOGNIZED", "REGISTERED")
MAX_BUCKETS = 1000


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Scope:
    since: datetime | None = None
    until: datetime | None = None
    source_id: str | None = None
    session_id: str | None = None

    def visit_conditions(self) -> list[ColumnElement[bool]]:
        conds: list[ColumnElement[bool]] = []
        if self.since:
            conds.append(Visit.entry_time >= aware(self.since))
        if self.until:
            conds.append(Visit.entry_time < aware(self.until))
        if self.source_id:
            conds.append(Visit.source_id == self.source_id)
        if self.session_id:
            conds.append(Visit.session_id == self.session_id)
        return conds

    def live_conditions(self) -> list[ColumnElement[bool]]:
        """Source/session filters only: 'who is in view now' has no date range."""
        conds: list[ColumnElement[bool]] = []
        if self.source_id:
            conds.append(Visit.source_id == self.source_id)
        if self.session_id:
            conds.append(Visit.session_id == self.session_id)
        return conds

    def session_conditions(self) -> list[ColumnElement[bool]]:
        conds: list[ColumnElement[bool]] = []
        if self.since:
            conds.append(ProcessingSession.created_at >= aware(self.since))
        if self.until:
            conds.append(ProcessingSession.created_at < aware(self.until))
        if self.source_id:
            conds.append(ProcessingSession.source_id == self.source_id)
        if self.session_id:
            conds.append(ProcessingSession.id == self.session_id)
        return conds

    def describe(self) -> dict[str, Any]:
        return {
            "since": self.since.isoformat() if self.since else None,
            "until": self.until.isoformat() if self.until else None,
            "source_id": self.source_id,
            "session_id": self.session_id,
        }


def _bucket_expr(db: Session, column: Any, bucket: str) -> Any:
    """UTC bucket start as a sortable string, on PostgreSQL and SQLite alike."""
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        return func.to_char(func.timezone("UTC", column), _pg_fmt(bucket))
    return func.strftime("%Y-%m-%dT%H:00:00" if bucket == "hour" else "%Y-%m-%dT00:00:00", column)


def _pg_fmt(bucket: str) -> str:
    return 'YYYY-MM-DD"T"HH24:00:00' if bucket == "hour" else 'YYYY-MM-DD"T"00:00:00'


def _parse_bucket(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)


class AnalyticsService:
    def __init__(
        self,
        factory: sessionmaker[Session],
        live_sessions: Callable[[], set[str]] = lambda: set(),
    ) -> None:
        self._factory = factory
        self._live = live_sessions
        self.timings: dict[str, float] = {}

    # ------------------------------------------------------------------ helpers
    def _scalar(self, db: Session, stmt: Select[Any]) -> int:
        return int(db.execute(stmt).scalar_one() or 0)

    def _visit_filter(self, stmt: Select[Any], scope: Scope) -> Select[Any]:
        for c in scope.visit_conditions():
            stmt = stmt.where(c)
        return stmt

    # ------------------------------------------------------------------ overview
    def overview(self, scope: Scope) -> dict[str, Any]:
        started = time.perf_counter()
        live = self._live()
        with self._factory() as db:
            registered = self._scalar(db, select(func.count()).select_from(Visitor))

            def base(*cols: Any) -> Select[Any]:
                return self._visit_filter(select(*cols).select_from(Visit), scope)

            # one pass over the visits in scope: visits, distinct identities, de-identified visits
            visits, unique, without_identity = (
                int(x or 0)
                for x in db.execute(
                    base(
                        func.count(),
                        func.count(distinct(Visit.visitor_id)),
                        func.sum(case((Visit.visitor_id.is_(None), 1), else_=0)),
                    )
                ).one()
            )
            # one pass over the events of those visits: entries and the kinds of terminal events
            entries = 0
            terminal = {"absence": 0, "observation_ended": 0, "interrupted": 0}
            for slot, etype, basis, n in db.execute(
                self._visit_filter(
                    select(VisitEvent.slot, VisitEvent.event_type, Visit.exit_basis, func.count())
                    .select_from(VisitEvent)
                    .join(Visit, Visit.id == VisitEvent.visit_id)
                    .group_by(VisitEvent.slot, VisitEvent.event_type, Visit.exit_basis),
                    scope,
                )
            ).all():
                if slot == "entry":
                    entries += int(n)
                else:
                    key = "interrupted" if etype == "interrupted" else (basis or "absence")
                    terminal[key] = terminal.get(key, 0) + int(n)
            returning = self._scalar(
                db,
                self._visit_filter(
                    select(func.count(distinct(Visit.visitor_id)))
                    .select_from(Visit)
                    .join(Visitor, Visitor.id == Visit.visitor_id)
                    .where(Visitor.first_session_id.is_not(None))
                    .where(Visit.session_id != Visitor.first_session_id),
                    scope,
                ),
            )
            # live view: open visits of LIVE sessions only; stale rows are a diagnostic
            open_stmt = select(Visit.state, Visit.session_id, func.count()).where(
                Visit.state.in_(OPEN_STATES)
            )
            for c in scope.live_conditions():
                open_stmt = open_stmt.where(c)
            active = stale = missing = 0
            for state, sid, n in db.execute(
                open_stmt.group_by(Visit.state, Visit.session_id)
            ).all():
                if sid in live:
                    active += int(n)
                    if state == "TEMPORARILY_MISSING":
                        missing += int(n)
                else:
                    stale += int(n)
            cov = self._coverage(db, scope)
            last_event = db.execute(select(func.max(VisitEvent.recorded_at))).scalar_one()
        out = {
            "scope": scope.describe(),
            "registered_visitors": registered,
            "unique_visitors_in_scope": unique,
            "returning_visitors_in_scope": returning,
            "new_visitors_in_scope": max(0, unique - returning),
            "visits": visits,
            "visits_without_identity": without_identity,
            "entry_events": entries,
            "terminal_events": sum(terminal.values()),
            "terminal_by_basis": terminal,
            "active_visits": active,
            "active_visits_temporarily_missing": missing,
            "stale_open_visits": stale,
            "unresolved_tracks": cov["tracks_unresolved"],
            "generated_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "last_event_recorded_at": aware(last_event).isoformat() if last_event else None,
            "definitions": DEFINITIONS,
        }
        self.timings["overview_ms"] = (time.perf_counter() - started) * 1000
        return out

    # ------------------------------------------------------------------ timeline
    def timeline(self, scope: Scope, bucket: str) -> dict[str, Any]:
        started = time.perf_counter()
        with self._factory() as db:
            ebucket = _bucket_expr(db, Visit.entry_time, bucket)
            entries: dict[str, dict[str, int]] = {}
            for b, visits, uniq in db.execute(
                self._visit_filter(
                    select(ebucket, func.count(), func.count(distinct(Visit.visitor_id)))
                    .select_from(Visit)
                    .group_by(ebucket),
                    scope,
                )
            ).all():
                entries[b] = {"visits": int(visits), "unique_visitors": int(uniq)}
            tbucket = _bucket_expr(db, VisitEvent.event_time, bucket)
            terminal: dict[str, dict[str, int]] = {}
            for b, etype, basis, n in db.execute(
                self._visit_filter(
                    select(tbucket, VisitEvent.event_type, Visit.exit_basis, func.count())
                    .select_from(VisitEvent)
                    .join(Visit, Visit.id == VisitEvent.visit_id)
                    .where(VisitEvent.slot == "terminal")
                    .group_by(tbucket, VisitEvent.event_type, Visit.exit_basis),
                    scope,
                )
            ).all():
                key = "interrupted" if etype == "interrupted" else (basis or "absence")
                terminal.setdefault(b, {})[key] = terminal.get(b, {}).get(key, 0) + int(n)
        keys = sorted(set(entries) | set(terminal))
        points: list[dict[str, Any]] = []
        if keys:
            step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
            cur, end = _parse_bucket(keys[0]), _parse_bucket(keys[-1])
            if (end - cur) / step >= MAX_BUCKETS:
                cur = end - step * (MAX_BUCKETS - 1)  # keep the newest buckets, say so
            truncated = _parse_bucket(keys[0]) < cur
            while cur <= end:
                k = cur.strftime("%Y-%m-%dT%H:%M:%S")
                e, t = entries.get(k, {}), terminal.get(k, {})
                points.append(
                    {
                        "bucket_start": cur.isoformat().replace("+00:00", "Z"),
                        "visits": e.get("visits", 0),
                        "entries": e.get("visits", 0),
                        "unique_visitors": e.get("unique_visitors", 0),
                        "exits_absence": t.get("absence", 0),
                        "exits_observation_ended": t.get("observation_ended", 0),
                        "interrupted": t.get("interrupted", 0),
                    }
                )
                cur += step
        else:
            truncated = False
        self.timings["timeline_ms"] = (time.perf_counter() - started) * 1000
        return {
            "scope": scope.describe(),
            "bucket": bucket,
            "timezone": "UTC",
            "truncated": truncated,
            "points": points,
            "note": "Buckets are UTC. Entries and unique visitors are bucketed by visit entry time; terminal "
            "events by their own time. Per-bucket unique visitors are NOT additive across buckets.",
        }

    # ------------------------------------------------------------------ breakdowns
    def sources(self, scope: Scope) -> dict[str, Any]:
        with self._factory() as db:
            rows = db.execute(
                self._visit_filter(
                    select(
                        Visit.source_id,
                        func.max(Visit.source_label),
                        func.count(),
                        func.count(distinct(Visit.visitor_id)),
                        func.count(distinct(Visit.session_id)),
                        func.max(Visit.entry_time),
                    )
                    .select_from(Visit)
                    .group_by(Visit.source_id)
                    .order_by(func.count().desc(), Visit.source_id),
                    scope,
                )
            ).all()
        return {
            "scope": scope.describe(),
            "items": [
                {
                    "source_id": sid,
                    "source_label": label,
                    "visits": int(v),
                    "unique_visitors": int(u),
                    "sessions": int(s),
                    "last_entry_at": aware(last).isoformat() if last else None,
                }
                for sid, label, v, u, s, last in rows
            ],
        }

    def sessions(self, scope: Scope, limit: int, offset: int) -> dict[str, Any]:
        with self._factory() as db:
            conds = scope.session_conditions()
            total = self._scalar(
                db,
                _where(select(func.count()).select_from(ProcessingSession), conds),
            )
            visit_agg = (
                select(
                    Visit.session_id.label("sid"),
                    func.count().label("visits"),
                    func.count(distinct(Visit.visitor_id)).label("uniq"),
                    func.sum(case((Visit.state == "INTERRUPTED", 1), else_=0)).label("interrupted"),
                )
                .group_by(Visit.session_id)
                .subquery()
            )
            reg_agg = (
                select(Visitor.first_session_id.label("sid"), func.count().label("registered"))
                .group_by(Visitor.first_session_id)
                .subquery()
            )
            rows = db.execute(
                _where(
                    select(
                        ProcessingSession.id,
                        ProcessingSession.source_id,
                        ProcessingSession.source_label,
                        ProcessingSession.state,
                        ProcessingSession.created_at,
                        ProcessingSession.ended_at,
                        func.coalesce(visit_agg.c.visits, 0),
                        func.coalesce(visit_agg.c.uniq, 0),
                        func.coalesce(visit_agg.c.interrupted, 0),
                        func.coalesce(reg_agg.c.registered, 0),
                    )
                    .select_from(ProcessingSession)
                    .outerjoin(visit_agg, visit_agg.c.sid == ProcessingSession.id)
                    .outerjoin(reg_agg, reg_agg.c.sid == ProcessingSession.id),
                    conds,
                )
                .order_by(ProcessingSession.created_at.desc(), ProcessingSession.id)
                .limit(limit)
                .offset(offset)
            ).all()
        live = self._live()
        return {
            "scope": scope.describe(),
            "total": total,
            "limit": limit,
            "offset": offset,
            "note": "A session row counts ALL of its visits (the session is the unit), not only those in the date range.",
            "items": [
                {
                    "session_id": sid,
                    "source_id": src,
                    "source_label": label,
                    "state": state,
                    "live": sid in live,
                    "started_at": aware(created).isoformat(),
                    "ended_at": aware(ended).isoformat() if ended else None,
                    "visits": int(v),
                    "unique_visitors": int(u),
                    "interrupted_visits": int(i),
                    "visitors_registered_here": int(r),
                }
                for sid, src, label, state, created, ended, v, u, i, r in rows
            ],
        }

    # ------------------------------------------------------------------ coverage
    def coverage(self, scope: Scope) -> dict[str, Any]:
        with self._factory() as db:
            return {"scope": scope.describe(), **self._coverage(db, scope)}

    def _coverage(self, db: Session, scope: Scope) -> dict[str, Any]:
        """Operational diagnostic: how many tracks ended with a resolved identity. NOT recall of people."""
        conds = scope.session_conditions()
        states = {
            state: int(n)
            for state, n in db.execute(
                _where(
                    select(IdentityAssociation.state, func.count())
                    .select_from(IdentityAssociation)
                    .join(ProcessingSession, ProcessingSession.id == IdentityAssociation.session_id)
                    .group_by(IdentityAssociation.state),
                    conds,
                )
            ).all()
        }
        resolved = sum(n for s, n in states.items() if s in RESOLVED)
        total = sum(states.values())
        sums: Counter[str] = Counter()
        sessions_n = 0
        for (summary,) in db.execute(
            _where(select(ProcessingSession.summary).select_from(ProcessingSession), conds).limit(
                5000
            )
        ):
            sessions_n += 1
            for key in (
                "tracks_confirmed_total",
                "recog_processed",
                "recog_usable",
                "recog_dropped",
                "recog_errors",
            ):
                value = (summary or {}).get(key)
                if isinstance(value, int | float):
                    sums[key] += int(value)
        rejected = max(0, sums["recog_processed"] - sums["recog_usable"])
        return {
            "sessions_in_scope": sessions_n,
            "confirmed_tracks": sums["tracks_confirmed_total"],
            "tracks_with_recognition_observations": total,
            "tracks_resolved": resolved,
            "tracks_unresolved": total - resolved,
            "tracks_by_state": states,
            "unresolved_breakdown": {
                "low_quality": states.get("INSUFFICIENT_QUALITY", 0),
                "undecided": states.get("UNRESOLVED", 0) + states.get("MATCH_PENDING", 0),
                "registration_pending": states.get("REGISTRATION_PENDING", 0),
                "ambiguous": states.get("AMBIGUOUS", 0),
                "rejected": states.get("REJECTED", 0),
            },
            "resolved_share_of_evaluated_tracks": round(resolved / total, 4) if total else None,
            "face_crops_analysed": sums["recog_processed"],
            "face_crops_usable": sums["recog_usable"],
            "face_crops_rejected_by_quality_gate": rejected,
            "face_crops_dropped_busy": sums["recog_dropped"],
            "recognition_errors": sums["recog_errors"],
            "disclaimer": "Operational diagnostic: the share of evaluated tracks that ended with a resolved identity. "
            "It is not person-level recall, tracks are not people, and unresolved tracks are never added to unique visitors.",
        }

    # ------------------------------------------------------------------ recent
    def recent(self, scope: Scope, limit: int) -> list[Any]:
        with self._factory() as db:
            stmt = (
                select(VisitEvent, Visit)
                .join(Visit, Visit.id == VisitEvent.visit_id)
                .order_by(VisitEvent.event_time.desc(), VisitEvent.slot.desc(), VisitEvent.id)
                .limit(limit)
            )
            for c in scope.visit_conditions():
                stmt = stmt.where(c)
            rows = db.execute(stmt).all()
            db.expunge_all()
            return list(rows)

    # ------------------------------------------------------------------ reconciliation (report only)
    def reconcile(self) -> dict[str, Any]:
        """Recompute the global numbers independently (Python, from raw rows) and compare with the SQL
        aggregates; check invariants. REPORTS differences - it never modifies data."""
        started = time.perf_counter()
        live = self._live()
        checks: list[dict[str, Any]] = []

        def check(
            name: str, expected: Any, actual: Any, detail: str = "", severity: str = "error"
        ) -> None:
            ok = expected == actual
            checks.append(
                {"name": name, "ok": ok, "expected": expected, "actual": actual, "detail": detail,
                 "severity": "info" if ok else severity}
            )  # fmt: skip

        scope = Scope()
        sql = self.overview(scope)
        with self._factory() as db:
            visitors = {v for (v,) in db.execute(select(Visitor.id))}
            visits = db.execute(
                select(
                    Visit.id,
                    Visit.visitor_id,
                    Visit.state,
                    Visit.session_id,
                    Visit.exit_basis,
                    Visit.exit_time,
                )
            ).all()
            events = db.execute(
                select(VisitEvent.id, VisitEvent.visit_id, VisitEvent.slot, VisitEvent.event_type,
                       VisitEvent.visitor_id, VisitEvent.image_status, VisitEvent.idempotency_key)
            ).all()  # fmt: skip
            sessions_state = {
                sid: st
                for sid, st in db.execute(select(ProcessingSession.id, ProcessingSession.state))
            }
            pending_outbox = self._scalar(
                db,
                select(func.count())
                .select_from(EventDeliveryOutbox)
                .where(EventDeliveryOutbox.status != "DELIVERED"),
            )
            visitors_without_embeddings = self._scalar(
                db,
                select(func.count())
                .select_from(Visitor)
                .where(
                    ~select(FaceEmbedding.id).where(FaceEmbedding.visitor_id == Visitor.id).exists()
                ),
            )
        visit_ids = {v[0] for v in visits}
        by_visit: dict[str, list[Any]] = {}
        for e in events:
            by_visit.setdefault(e[1], []).append(e)
        check(
            "registered_visitors",
            len(visitors),
            sql["registered_visitors"],
            "COUNT(visitors) vs rows",
        )
        check(
            "unique_visitors_global",
            len({v[1] for v in visits if v[1]}),
            sql["unique_visitors_in_scope"],
            "COUNT(DISTINCT visits.visitor_id) vs set of visit rows",
        )
        check("visits", len(visits), sql["visits"], "COUNT(visits) vs rows")
        check(
            "visits_without_identity",
            sum(1 for v in visits if v[1] is None),
            sql["visits_without_identity"],
        )
        check(
            "entry_events",
            sum(1 for e in events if e[2] == "entry"),
            sql["entry_events"],
            "entry events vs SQL",
        )
        check(
            "terminal_events",
            sum(1 for e in events if e[2] == "terminal"),
            sql["terminal_events"],
            "terminal events vs SQL",
        )
        bad_counts = []
        for v in visits:
            evs = by_visit.get(v[0], [])
            entries = sum(1 for e in evs if e[2] == "entry")
            terms = sum(1 for e in evs if e[2] == "terminal")
            closed = v[2] in ("CLOSED", "INTERRUPTED")
            if entries != 1 or terms != (1 if closed else 0):
                bad_counts.append(v[0])
        check(
            "every_visit_has_one_entry_and_the_right_terminal_events",
            0,
            len(bad_counts),
            ",".join(bad_counts[:5]),
        )
        check("events_without_visit", 0, sum(1 for e in events if e[1] not in visit_ids))
        check(
            "visits_with_unknown_visitor",
            0,
            sum(1 for v in visits if v[1] and v[1] not in visitors),
            "foreign key integrity",
        )
        keys = Counter(e[6] for e in events)
        check("duplicate_idempotency_keys", 0, sum(1 for n in keys.values() if n > 1))
        mismatch = 0
        by_id = {v[0]: v for v in visits}
        for e in events:
            if e[2] != "terminal" or e[1] not in by_id:
                continue
            basis = by_id[e[1]][4]
            if (e[3] == "interrupted") != (basis == "interrupted"):
                mismatch += 1
        check("terminal_event_type_matches_closure_basis", 0, mismatch)
        check(
            "exit_time_set_exactly_for_final_visits",
            0,
            sum(1 for v in visits if (v[2] in ("CLOSED", "INTERRUPTED")) != (v[5] is not None)),
        )
        anonymised = sum(
            1 for e in events if e[4] is None and e[5] not in ("ERASED", "UNAVAILABLE", "MISSING")
        )
        check(
            "erased_events_keep_no_evidence_reference",
            0,
            anonymised,
            "events of erased visitors must not claim a stored image",
        )
        stale = sum(1 for v in visits if v[2] in OPEN_STATES and v[3] not in live)
        check(
            "stale_open_visits_reported_not_counted_as_active",
            sql["stale_open_visits"],
            stale,
            "open visits of sessions that are not live (run event reconciliation)",
            severity="warning",
        )
        check(
            "active_visits",
            sum(1 for v in visits if v[2] in OPEN_STATES and v[3] in live),
            sql["active_visits"],
        )
        live_dead = [s for s in live if sessions_state.get(s) not in ACTIVE_STATES]
        check("live_sessions_are_active_in_the_database", 0, len(live_dead), severity="warning")
        check(
            "events_log_deliveries_pending",
            0,
            pending_outbox,
            "outbox rows not yet DELIVERED (see /events/health)",
            severity="warning",
        )
        check(
            "visitors_without_embeddings",
            0,
            visitors_without_embeddings,
            "identity without any template",
            severity="warning",
        )
        errors = [c for c in checks if not c["ok"] and c["severity"] == "error"]
        warnings = [c for c in checks if not c["ok"] and c["severity"] == "warning"]
        return {
            "consistent": not errors,
            "errors": len(errors),
            "warnings": len(warnings),
            "checks": checks,
            "mode": "report-only: no data was modified",
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }


def _where(stmt: Select[Any], conds: list[ColumnElement[bool]]) -> Select[Any]:
    for c in conds:
        stmt = stmt.where(c)
    return stmt


DEFINITIONS = {
    "registered_visitors": "Persistent face identities currently registered (global, ignores the filters). Erased visitors are not counted.",
    "unique_visitors_in_scope": "Distinct registered identities with at least one visit entering in scope. Faces that were not reliably recognised are not included.",
    "returning_visitors_in_scope": "Unique visitors in scope with an in-scope visit in a session other than the one in which they were first registered.",
    "visits": "Presence episodes (one visitor, one session). The same visitor can have many.",
    "entry_events": "Logical entry events of the visits in scope (one per visit).",
    "terminal_events": "Logical ends of the visits in scope: exits (left the view / observation ended) and interruptions. None is a verified physical departure.",
    "active_visits": "Open visits of live sessions right now (date range ignored). Open rows of sessions that are not live are stale and not counted.",
    "unresolved_tracks": "Historical diagnostic: evaluated tracks without a resolved identity. Never extra people.",
}
