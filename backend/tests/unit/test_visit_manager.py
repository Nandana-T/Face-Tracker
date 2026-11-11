"""Visit lifecycle state machine: pure logic, deterministic, no database / files / clock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import VisitsConfig
from app.services.visit_manager import (
    TrackSnap,
    VisitAction,
    VisitManager,
    event_id_for,
    visit_id_for,
)

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
S = "s" * 32
A, B = "a" * 32, "b" * 32


def wall(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)


def snap(
    track: int, *, observed: bool = True, state: str = "CONFIRMED", first: int = 0
) -> TrackSnap:
    return TrackSnap(track, state if observed or state == "LOST" else state, observed, first)


class Rig:
    """Drives one session at 10 frames/second."""

    def __init__(self, **cfg: float) -> None:
        defaults = {
            "missing_after_seconds": 1.5,
            "exit_after_seconds": 8,
            "max_frame_gap_seconds": 2,
        }
        self.m = VisitManager(VisitsConfig(**{**defaults, **cfg}))
        self.m.begin_session(S, "clip", "Clip")
        self.ms = 0
        self.fid = 0
        self.actions: list[VisitAction] = []

    def frame(self, *tracks: TrackSnap, step: int = 100) -> list[VisitAction]:
        self.ms += step
        self.fid += 1
        out = self.m.on_frame(S, self.fid, self.ms, wall(self.ms), tracks)
        self.actions += out
        return out

    def run(self, seconds: float, *tracks: TrackSnap) -> list[VisitAction]:
        out: list[VisitAction] = []
        for _ in range(int(seconds * 10)):
            out += self.frame(*tracks)
        return out

    def identify(self, track: int, visitor: str | None, number: int = 1) -> list[VisitAction]:
        out = self.m.on_identity(S, track, visitor, number, self.ms, wall(self.ms))
        self.actions += out
        return out

    def crop(self, track: int, quality: float = 0.8, tag: bytes = b"x") -> None:
        self.m.on_observation(S, track, b"\xff\xd8" + tag, quality, self.ms)

    def of(self, kind: str) -> list[VisitAction]:
        return [a for a in self.actions if a.kind == kind]


def test_unconfirmed_track_is_only_a_candidate_and_creates_no_visit() -> None:
    r = Rig()
    r.identify(1, A)
    r.run(2, snap(1, state="TENTATIVE"))
    assert r.actions == [] and r.m.active(S) == []


def test_confirmed_identified_presence_opens_exactly_one_visit() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.crop(1)
    r.identify(1, A)
    assert [a.kind for a in r.actions] == ["entry"]
    r.run(5, snap(1))  # many more sightings, repeated identity messages
    r.identify(1, A)
    r.identify(1, A)
    assert [a.kind for a in r.of("entry")] == ["entry"] and len(r.of("terminal")) == 0
    assert len(r.m.active(S)) == 1


def test_identity_found_late_gets_the_earliest_sighting_as_entry_time() -> None:
    r = Rig()
    r.run(4, snap(1, first=100))  # tracked since 100 ms, identity unknown
    r.identify(1, A)
    entry = r.of("entry")[0]
    assert entry.visit.entry_video_ms == 100  # not "now" (4000 ms)
    assert entry.visit.entry_wall == wall(100)


def test_entry_image_is_the_best_good_crop_and_poor_crops_are_ignored() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.crop(1, 0.5, b"mid")
    r.crop(1, 0.9, b"best")
    r.crop(1, 0.1, b"poor")  # below the minimum evidence quality
    r.crop(1, 0.6, b"later")
    r.identify(1, A)
    entry = r.of("entry")[0]
    assert entry.crop is not None and entry.crop.jpeg.endswith(b"best")
    assert entry.image_kind == "observation"


def test_no_crop_means_explicitly_no_evidence_not_a_fabricated_one() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.identify(1, A)
    assert r.of("entry")[0].crop is None and r.of("entry")[0].image_kind is None


def test_short_disappearance_keeps_the_visit_and_logs_nothing() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.identify(1, A)
    r.run(1.0, snap(1, observed=False, state="LOST"))  # below missing_after
    out = r.run(1, snap(1))
    assert r.of("terminal") == [] and len(r.of("entry")) == 1
    assert [a.visit.state for a in out if a.kind == "state"] == []


def test_missing_then_reacquired_before_exit_does_not_create_events() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.identify(1, A)
    gone = r.run(3, snap(1, observed=False, state="LOST"))
    assert [a.visit.state for a in gone if a.kind == "state"] == ["TEMPORARILY_MISSING"]
    back = r.run(1, snap(1))
    assert [a.visit.state for a in back if a.kind == "state"] == ["ACTIVE"]
    assert len(r.of("entry")) == 1 and r.of("terminal") == []
    assert r.m.stats["visits_reacquired"] == 1


def test_long_disappearance_closes_with_one_exit_at_the_last_observation() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.identify(1, A)
    last_seen = r.ms
    r.run(12, snap(1, observed=False, state="LOST"))
    exits = r.of("terminal")
    assert len(exits) == 1
    ex = exits[0]
    assert ex.event_type == "exit" and ex.visit.exit_basis == "absence"
    assert ex.visit.closure_reason == "absence_timeout"
    # The exit time is the last sighting, not the moment the absence was recognised.
    assert ex.visit.last_video_ms == last_seen and ex.visit.last_wall == wall(last_seen)
    assert r.m.active(S) == []
    r.run(20, snap(1, observed=False, state="LOST"))
    assert len(r.of("terminal")) == 1  # never a second exit


def test_exit_image_is_the_last_good_observation() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.crop(1, 0.9, b"first")
    r.identify(1, A)
    r.run(1, snap(1))
    r.crop(1, 0.5, b"latest")
    r.run(10, snap(1, observed=False, state="LOST"))
    ex = r.of("terminal")[0]
    assert ex.crop is not None and ex.crop.jpeg.endswith(b"latest")
    assert ex.image_kind == "last_good_observation"


def test_new_track_of_the_same_visitor_continues_the_visit() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.identify(1, A)
    r.run(3, snap(1, observed=False, state="LOST"))
    r.run(1, snap(2, first=r.ms))  # a brand new track id ...
    r.identify(2, A)  # ... recognised as the same person
    r.run(1, snap(2))
    assert len(r.of("entry")) == 1 and r.of("terminal") == []
    assert r.m.active(S)[0].track_segments == 2


def test_returning_visitor_after_a_finalised_exit_gets_a_new_visit_same_visitor() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.identify(1, A)
    r.run(12, snap(1, observed=False, state="LOST"))
    assert len(r.of("terminal")) == 1
    r.run(1, snap(5, first=r.ms))
    r.identify(5, A)
    r.run(1, snap(5))
    entries = r.of("entry")
    assert len(entries) == 2
    assert entries[0].visit.visitor_id == entries[1].visit.visitor_id == A
    assert entries[0].visit.id != entries[1].visit.id  # visit_id != visitor_id, one per episode


def test_two_visitors_are_handled_independently() -> None:
    r = Rig()
    r.run(2, snap(1), snap(2))
    r.identify(1, A, 1)
    r.identify(2, B, 2)
    assert {a.visit.visitor_id for a in r.of("entry")} == {A, B}
    r.run(12, snap(2))  # A leaves, B stays
    exits = r.of("terminal")
    assert [x.visit.visitor_id for x in exits] == [A]
    assert [v.visitor_id for v in r.m.active(S)] == [B]


def test_duplicate_and_out_of_order_frames_are_ignored() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.identify(1, A)
    before = dict(r.m.stats)
    assert r.m.on_frame(S, 3, r.ms, wall(r.ms), [snap(1)]) == []  # old frame id
    assert r.m.on_frame(S, r.fid, r.ms, wall(r.ms), [snap(1)]) == []  # same frame again
    assert (
        r.m.on_frame(S, r.fid + 1, r.ms - 500, wall(r.ms), [snap(1)]) == []
    )  # time went backwards
    assert r.m.stats["stale_frames_ignored"] == before["stale_frames_ignored"] + 3
    assert len(r.of("entry")) == 1


def test_a_stall_between_frames_is_blind_time_not_absence() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.identify(1, A)
    out = r.frame(snap(1, observed=False, state="LOST"), step=60_000)  # one minute with no frames
    assert out == [] and r.of("terminal") == []
    assert r.m.stats["blind_gaps"] == 1


def test_source_reconnection_does_not_count_as_absence() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.identify(1, A)
    r.m.on_connection(S, "reconnecting")
    r.ms += 30_000
    out = r.frame(snap(1, observed=False, state="LOST"), step=0)
    assert out == [] and r.of("terminal") == []


@pytest.mark.parametrize(
    ("reason", "event_type", "basis"),
    [
        ("end_of_stream", "exit", "observation_ended"),
        ("manual_stop", "exit", "observation_ended"),
        ("worker_lost", "interrupted", "interrupted"),
        ("api_shutdown", "interrupted", "interrupted"),
        ("source_error", "interrupted", "interrupted"),
    ],
)
def test_session_end_rules(reason: str, event_type: str, basis: str) -> None:
    r = Rig()
    r.run(2, snap(1))
    r.crop(1, 0.7, b"c")
    r.identify(1, A)
    r.run(1, snap(1))
    actions = r.m.end_session(S, reason, wall(r.ms))
    assert len(actions) == 1
    assert actions[0].event_type == event_type and actions[0].visit.exit_basis == basis
    assert actions[0].visit.closure_reason == reason
    # The end time is the last sighting - an ended stream is not a physical departure.
    assert actions[0].visit.last_video_ms == r.ms
    assert r.m.end_session(S, reason, wall(r.ms)) == []  # idempotent


def test_session_end_discards_unconfirmed_candidates_without_events() -> None:
    r = Rig()
    r.identify(1, A)
    r.run(1, snap(1, state="TENTATIVE"))
    assert r.m.end_session(S, "end_of_stream", wall(r.ms)) == []
    assert r.m.stats["candidates_discarded"] == 1


def test_identity_reassignment_moves_the_track_and_drops_old_crops() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.crop(1, 0.9, b"maybe-someone-else")
    r.identify(1, A)
    r.identify(1, B, 2)  # the track is now believed to be B
    entries = r.of("entry")
    assert [e.visit.visitor_id for e in entries] == [A, B]
    assert entries[1].crop is None  # evidence captured before the change is not reused


def test_identity_loss_unbinds_the_track() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.identify(1, A)
    r.identify(1, None)
    r.run(12, snap(1))
    assert len(r.of("terminal")) == 1  # nobody is bound any more, so the visit ends by absence


def test_open_visit_last_observation_is_persisted_periodically() -> None:
    r = Rig()
    r.run(1, snap(1))
    r.identify(1, A)
    out = r.run(65, snap(1))
    touches = [a for a in out if a.kind == "touch"]
    assert 1 <= len(touches) <= 3
    assert touches[0].visit.last_video_ms >= 30_000


def test_ids_are_deterministic_and_distinct() -> None:
    k1, k2 = f"{S}:{A}:1", f"{S}:{A}:2"
    assert visit_id_for(k1) == visit_id_for(k1) != visit_id_for(k2)
    v = visit_id_for(k1)
    assert event_id_for(v, "entry") != event_id_for(v, "terminal")
    assert len(v) == 32 and len(event_id_for(v, "entry")) == 32


def test_config_rejects_exit_before_missing() -> None:
    with pytest.raises(ValueError, match="exit_after_seconds"):
        VisitsConfig(missing_after_seconds=10, exit_after_seconds=5)


def test_forgetting_an_erased_visitor_keeps_the_open_visit_but_unbinds_the_track() -> None:
    r = Rig()
    r.run(2, snap(1))
    r.crop(1, 0.8, b"face")
    r.identify(1, A)
    r.m.forget_visitor(A)
    assert [v.visitor_id for v in r.m.active(S)] == [A]  # the visit (audit trail) is not discarded
    r.run(12, snap(1))  # the face is still there but nobody is bound any more ...
    terminals = r.of("terminal")
    assert len(terminals) == 1  # ... so the visit ends by absence of a bound identity
    assert terminals[0].crop is None  # and the erased visitor's crops were dropped
