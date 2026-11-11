"""Visit lifecycle state machine (Phase 4) - pure logic: no database, no files, no wall clock.

Identifiers (never interchangeable)
  track_id   temporary trajectory inside one session (tracker)
  visitor_id persistent facial identity (identity manager)
  visit_id   one confirmed presence episode of one visitor (this module)

A visit belongs to a (session, visitor). It may consist of several track segments: when a track
disappears the visit survives, and a NEW track that is recognised as the same visitor continues it.
A track disappearing is therefore not an exit, and a new track id does not create a visit.

States and transitions (CANDIDATE exists only in memory and never produces an event)

    (identity bound to a track)          CANDIDATE
    CANDIDATE --track CONFIRMED & seen--> ACTIVE                 emits ENTRY
    ACTIVE --not seen >= missing_after--> TEMPORARILY_MISSING
    TEMPORARILY_MISSING --seen again----> ACTIVE                 (no event: the visit continues)
    ACTIVE|TEMPORARILY_MISSING --not seen >= exit_after--> EXIT_PENDING   emits EXIT (absence)
    ACTIVE|TEMPORARILY_MISSING --stream ended / manual stop--> EXIT_PENDING emits EXIT (observation_ended)
    ACTIVE|TEMPORARILY_MISSING --worker lost / shutdown / error--> EXIT_PENDING emits INTERRUPTED
    EXIT_PENDING --persistence acknowledged--> CLOSED | INTERRUPTED

Time. Absence is accumulated in VIDEO time between PROCESSED frames. A gap longer than
``max_frame_gap_seconds`` (worker stall, RTSP outage) is blind time, never absence; frames that
arrive out of order or twice are ignored. The exit time is the LAST defensible observation, not an
invented departure moment; the moment the exit was decided is recorded separately by the recorder.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.core.config import VisitsConfig

# Why a session ended -> how its open visits are closed.
OBSERVATION_ENDED = ("end_of_stream", "manual_stop")
MAX_TRACK_CROPS = 512
TOUCH_INTERVAL_MS = 30_000  # how often an open visit's last observation is persisted


def visit_id_for(visit_key: str) -> str:
    return hashlib.sha256(f"visit:{visit_key}".encode()).hexdigest()[:32]


def event_id_for(visit_id: str, slot: str) -> str:
    return hashlib.sha256(f"event:{visit_id}:{slot}".encode()).hexdigest()[:32]


@dataclass(frozen=True)
class TrackSnap:
    track_id: int
    state: str  # TENTATIVE | CONFIRMED | LOST
    observed: bool
    first_seen_ms: int


@dataclass(frozen=True)
class Crop:
    jpeg: bytes
    quality: float
    video_ms: int


@dataclass(frozen=True)
class VisitSnapshot:
    id: str
    visit_key: str
    session_id: str
    source_id: str
    source_label: str
    visitor_id: str
    visitor_number: int
    state: str
    entry_video_ms: int
    entry_wall: datetime
    last_video_ms: int
    last_wall: datetime
    track_segments: int
    exit_basis: str | None = None
    closure_reason: str | None = None


@dataclass(frozen=True)
class VisitAction:
    kind: Literal["entry", "terminal", "state", "touch"]
    visit: VisitSnapshot
    event_type: Literal["entry", "exit", "interrupted"] | None = None
    crop: Crop | None = None
    image_kind: str | None = None  # observation | last_good_observation


@dataclass
class _TrackCrops:
    best: Crop | None = None
    last: Crop | None = None


@dataclass
class _Visit:
    id: str
    key: str
    visitor_id: str
    number: int
    entry_ms: int
    entry_wall: datetime
    last_ms: int
    last_wall: datetime
    state: str = "ACTIVE"
    tracks: set[int] = field(default_factory=set)
    segments: int = 1
    absent_ms: int = 0
    persisted_ms: int = 0  # video position of the last persisted observation
    exit_basis: str | None = None
    closure_reason: str | None = None


@dataclass
class _Session:
    source_id: str
    source_label: str
    last_frame_id: int = -1
    prev_ms: int | None = None
    snap: dict[int, TrackSnap] = field(default_factory=dict)
    first_wall: dict[int, datetime] = field(default_factory=dict)
    bound: dict[int, tuple[str, int]] = field(default_factory=dict)  # track -> (visitor, number)
    crops: OrderedDict[int, _TrackCrops] = field(default_factory=OrderedDict)
    visits: dict[str, _Visit] = field(default_factory=dict)  # open visits by visitor
    closing: dict[str, _Visit] = field(default_factory=dict)  # decided, not yet acknowledged
    ordinals: dict[str, int] = field(default_factory=dict)


class VisitManager:
    def __init__(self, cfg: VisitsConfig) -> None:
        self.cfg = cfg
        self._sessions: dict[str, _Session] = {}
        self.stats = {
            "stale_frames_ignored": 0,
            "blind_gaps": 0,
            "visits_opened": 0,
            "visits_reacquired": 0,
            "visits_closed": 0,
            "candidates_discarded": 0,
        }

    # ------------------------------------------------------------------ session scope
    def begin_session(self, session_id: str, source_id: str, source_label: str) -> None:
        self._sessions.setdefault(session_id, _Session(source_id, source_label))

    def on_connection(self, session_id: str, state: str) -> None:
        """RTSP reconnecting/connected: forget the previous frame time so the outage is not absence."""
        s = self._sessions.get(session_id)
        if s is not None:
            s.prev_ms = None

    # ------------------------------------------------------------------ inputs
    def on_frame(
        self,
        session_id: str,
        frame_id: int,
        video_ms: int,
        wall: datetime,
        tracks: Iterable[TrackSnap],
    ) -> list[VisitAction]:
        s = self._sessions.get(session_id)
        if s is None:
            return []
        if frame_id <= s.last_frame_id or (s.prev_ms is not None and video_ms < s.prev_ms):
            self.stats["stale_frames_ignored"] += 1  # duplicate / out-of-order message
            return []
        s.last_frame_id = frame_id
        s.snap = {t.track_id: t for t in tracks}
        for tid in s.snap:
            s.first_wall.setdefault(tid, wall)
        actions = self._evaluate(s, session_id, video_ms, wall, advance=True)
        s.prev_ms = video_ms
        return actions

    def on_observation(
        self, session_id: str, track_id: int, jpeg: bytes | None, quality: float, video_ms: int
    ) -> None:
        """Remember the best / latest GOOD face crop of a track (the recognition quality gate has
        already filtered it; a crop below ``min_crop_quality`` is still refused)."""
        s = self._sessions.get(session_id)
        if s is None or not jpeg or quality < self.cfg.min_crop_quality:
            return
        entry = s.crops.get(track_id)
        if entry is None:
            entry = s.crops[track_id] = _TrackCrops()
            if len(s.crops) > MAX_TRACK_CROPS:
                s.crops.popitem(last=False)
        crop = Crop(jpeg, quality, video_ms)
        entry.last = crop
        if entry.best is None or quality >= entry.best.quality:
            entry.best = crop

    def on_identity(
        self,
        session_id: str,
        track_id: int,
        visitor_id: str | None,
        visitor_number: int | None,
        video_ms: int,
        wall: datetime,
    ) -> list[VisitAction]:
        s = self._sessions.get(session_id)
        if s is None:
            return []
        previous = s.bound.get(track_id)
        if visitor_id is None:
            if previous is not None:
                self._unbind(s, track_id)
            return []
        if previous is not None and previous[0] != visitor_id:
            self._unbind(s, track_id)
            s.crops.pop(track_id, None)  # older crops may show somebody else
        s.bound[track_id] = (visitor_id, visitor_number or 0)
        return self._evaluate(s, session_id, video_ms, wall, advance=False)

    def end_session(self, session_id: str, reason: str, wall: datetime) -> list[VisitAction]:
        s = self._sessions.pop(session_id, None)
        if s is None:
            return []
        if reason in OBSERVATION_ENDED:
            event_type: Literal["exit", "interrupted"] = "exit"
            basis = "observation_ended"
        else:
            event_type, basis = "interrupted", "interrupted"
        actions = []
        for visitor_id in list(s.visits):
            actions.append(
                self._close(s, session_id, s.visits[visitor_id], event_type, basis, reason)
            )
        bound_unopened = {v for v, _ in s.bound.values()} - set(s.closing)
        self.stats["candidates_discarded"] += len(
            bound_unopened - {a.visit.visitor_id for a in actions}
        )
        return actions

    def acknowledge(self, session_id: str, visit_id: str) -> None:
        """The terminal event is durably recorded: EXIT_PENDING -> CLOSED / INTERRUPTED."""
        s = self._sessions.get(session_id)
        if s is None:
            return
        for visitor_id, v in list(s.closing.items()):
            if v.id == visit_id:
                v.state = "INTERRUPTED" if v.exit_basis == "interrupted" else "CLOSED"
                del s.closing[visitor_id]

    def forget_visitor(self, visitor_id: str) -> None:
        """The visitor was erased: no track is bound to them any more. An open visit is not discarded
        (that would silently lose the audit trail); it ends by absence and is recorded de-identified."""
        for s in self._sessions.values():
            for tid in [t for t, (v, _n) in s.bound.items() if v == visitor_id]:
                s.bound.pop(tid, None)
                s.crops.pop(tid, None)

    # ------------------------------------------------------------------ views
    def active(self, session_id: str) -> list[VisitSnapshot]:
        s = self._sessions.get(session_id)
        if s is None:
            return []
        return [self._snap(session_id, s, v) for v in s.visits.values()]

    # ------------------------------------------------------------------ internals
    def _unbind(self, s: _Session, track_id: int) -> None:
        old = s.bound.pop(track_id, None)
        if old is not None and old[0] in s.visits:
            s.visits[old[0]].tracks.discard(track_id)

    def _evaluate(
        self, s: _Session, session_id: str, video_ms: int, wall: datetime, *, advance: bool
    ) -> list[VisitAction]:
        actions: list[VisitAction] = []
        seen: dict[str, list[TrackSnap]] = {}
        for tid, (visitor, _n) in s.bound.items():
            snap = s.snap.get(tid)
            if snap is not None and snap.observed and snap.state != "LOST":
                seen.setdefault(visitor, []).append(snap)

        for visitor, snaps in seen.items():
            visit = s.visits.get(visitor)
            if visit is None:
                if not any(t.state == "CONFIRMED" for t in snaps):
                    continue  # still only a candidate
                actions.append(self._open(s, session_id, visitor, snaps, video_ms, wall))
                continue
            if advance:
                visit.last_ms, visit.last_wall, visit.absent_ms = video_ms, wall, 0
            for t in snaps:
                if t.track_id not in visit.tracks:
                    visit.tracks.add(t.track_id)
                    visit.segments += 1
            if visit.state == "TEMPORARILY_MISSING":
                visit.state = "ACTIVE"
                self.stats["visits_reacquired"] += 1
                actions.append(VisitAction("state", self._snap(session_id, s, visit)))
            if advance and visit.last_ms - visit.persisted_ms >= TOUCH_INTERVAL_MS:
                visit.persisted_ms = visit.last_ms
                actions.append(VisitAction("touch", self._snap(session_id, s, visit)))

        if advance:
            delta = 0
            if s.prev_ms is not None:
                raw = video_ms - s.prev_ms
                if raw > self.cfg.max_frame_gap_seconds * 1000:
                    self.stats["blind_gaps"] += 1  # a stall is not absence
                else:
                    delta = max(0, raw)
            for visitor in list(s.visits):
                if visitor in seen:
                    continue
                visit = s.visits[visitor]
                visit.absent_ms += delta
                if visit.absent_ms >= self.cfg.exit_after_seconds * 1000:
                    actions.append(
                        self._close(s, session_id, visit, "exit", "absence", "absence_timeout")
                    )
                elif (
                    visit.absent_ms >= self.cfg.missing_after_seconds * 1000
                    and visit.state == "ACTIVE"
                ):
                    visit.state = "TEMPORARILY_MISSING"
                    actions.append(VisitAction("state", self._snap(session_id, s, visit)))
        return actions

    def _open(
        self,
        s: _Session,
        session_id: str,
        visitor: str,
        snaps: list[TrackSnap],
        video_ms: int,
        wall: datetime,
    ) -> VisitAction:
        # Identity may be discovered long after tracking began: the visit starts at the earliest
        # sighting of the track(s) that carry this visitor, not at the moment of recognition.
        first = min(snaps, key=lambda t: t.first_seen_ms)
        entry_ms = min(first.first_seen_ms, video_ms)
        entry_wall = min(s.first_wall.get(first.track_id, wall), wall)
        ordinal = s.ordinals.get(visitor, 0) + 1
        s.ordinals[visitor] = ordinal
        key = f"{session_id}:{visitor}:{ordinal}"
        number = next((n for tid, (v, n) in s.bound.items() if v == visitor), 0)
        visit = _Visit(
            visit_id_for(key), key, visitor, number, entry_ms, entry_wall, video_ms, wall,
            persisted_ms=video_ms, tracks={t.track_id for t in snaps}, segments=len(snaps),
        )  # fmt: skip
        s.visits[visitor] = visit
        self.stats["visits_opened"] += 1
        crop = self._best_crop(s, visit.tracks)
        return VisitAction(
            "entry",
            self._snap(session_id, s, visit),
            "entry",
            crop,
            "observation" if crop else None,
        )

    def _close(
        self,
        s: _Session,
        session_id: str,
        visit: _Visit,
        event_type: Literal["exit", "interrupted"],
        basis: str,
        reason: str,
    ) -> VisitAction:
        visit.state = "EXIT_PENDING"
        visit.exit_basis, visit.closure_reason = basis, reason
        s.visits.pop(visit.visitor_id, None)
        s.closing[visit.visitor_id] = visit
        self.stats["visits_closed"] += 1
        tracks = visit.tracks | {t for t, (v, _n) in s.bound.items() if v == visit.visitor_id}
        crop = self._last_crop(s, tracks)
        return VisitAction(
            "terminal",
            self._snap(session_id, s, visit),
            event_type,
            crop,
            "last_good_observation" if crop else None,
        )

    @staticmethod
    def _best_crop(s: _Session, tracks: set[int]) -> Crop | None:
        found = [c.best for t, c in s.crops.items() if t in tracks and c.best is not None]
        return max(found, key=lambda c: (c.quality, c.video_ms), default=None)

    @staticmethod
    def _last_crop(s: _Session, tracks: set[int]) -> Crop | None:
        found = [c.last for t, c in s.crops.items() if t in tracks and c.last is not None]
        return max(found, key=lambda c: c.video_ms, default=None)

    @staticmethod
    def _snap(session_id: str, s: _Session, v: _Visit) -> VisitSnapshot:
        return VisitSnapshot(
            id=v.id,
            visit_key=v.key,
            session_id=session_id,
            source_id=s.source_id,
            source_label=s.source_label,
            visitor_id=v.visitor_id,
            visitor_number=v.number,
            state=v.state,
            entry_video_ms=v.entry_ms,
            entry_wall=v.entry_wall,
            last_video_ms=v.last_ms,
            last_wall=v.last_wall,
            track_segments=v.segments,
            exit_basis=v.exit_basis,
            closure_reason=v.closure_reason,
        )
