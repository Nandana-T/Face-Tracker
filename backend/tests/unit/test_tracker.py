"""Deterministic unit tests for the Phase 2 face tracker (no model, no video)."""

from __future__ import annotations

import math
from typing import Any

import pytest

from app.vision.detection.interfaces import FaceDetection
from app.vision.tracking.interfaces import TrackerOutput, TrackState
from app.vision.tracking.kalman import BoxFilter
from app.vision.tracking.tracker import (
    FaceByteTracker,
    TrackerConfig,
    greedy_match,
    iou_matrix,
)

STEP = 120  # ms between analysed frames: 25 fps with frame_skip=2
SIZE = (1000, 600)


def det(x: float, y: float, w: float = 80, h: float = 100, conf: float = 0.9) -> FaceDetection:
    return FaceDetection(x, y, x + w, y + h, conf)


class Run:
    """Feeds a tracker one analysed frame at a time with a monotonically advancing clock."""

    def __init__(self, **cfg: Any) -> None:
        self.tracker = FaceByteTracker(TrackerConfig(**cfg))
        self.ts = 0
        self.frame = 0
        self.last: TrackerOutput | None = None

    def step(self, dets: list[FaceDetection], dt_ms: int = STEP) -> TrackerOutput:
        out = self.tracker.update(dets, self.ts, self.frame, SIZE)
        self.ts += dt_ms
        self.frame += 3
        self.last = out
        return out


def states(out: TrackerOutput) -> dict[int, TrackState]:
    return {t.track_id: t.state for t in out.tracks}


# ---------------------------------------------------------------------------- basics
def test_empty_detections_produce_no_tracks() -> None:
    run = Run()
    for _ in range(5):
        out = run.step([])
    assert out.tracks == []
    assert out.counts.created_total == 0


def test_single_face_lifecycle_tentative_then_confirmed() -> None:
    run = Run(confirm_hits=3)
    seen = [states(run.step([det(100, 100)])).get(1) for _ in range(4)]
    assert seen == [
        TrackState.TENTATIVE,
        TrackState.TENTATIVE,
        TrackState.CONFIRMED,
        TrackState.CONFIRMED,
    ]
    assert run.last is not None
    assert run.last.counts.confirmed_total == 1


def test_track_fields_for_an_observed_track() -> None:
    run = Run()
    for _ in range(3):
        out = run.step([det(100, 100, conf=0.83)])
    (t,) = out.tracks
    assert t.observed and t.detection_confidence == pytest.approx(0.83)
    assert t.first_seen_ms == 0 and t.last_seen_ms == 2 * STEP and t.age_ms == 2 * STEP
    assert t.hits == 3 and t.missed_updates == 0 and t.last_update_frame == 6


def test_multiple_independent_faces_get_distinct_stable_ids() -> None:
    run = Run()
    ids = None
    for i in range(10):
        out = run.step([det(50 + 4 * i, 100), det(400 - 3 * i, 300), det(800, 50 + 2 * i)])
        current = sorted(t.track_id for t in out.tracks)
        ids = ids or current
        assert current == ids
    assert ids is not None and len(set(ids)) == 3
    assert all(t.state is TrackState.CONFIRMED for t in out.tracks)


def test_low_confidence_detection_cannot_start_a_track() -> None:
    run = Run(high_confidence=0.6)
    for _ in range(5):
        out = run.step([det(100, 100, conf=0.55)])
    assert out.tracks == [] and out.counts.created_total == 0


def test_low_confidence_detection_extends_an_existing_track() -> None:
    run = Run(high_confidence=0.6)
    for _ in range(3):
        run.step([det(100, 100, conf=0.9)])
    out = run.step([det(102, 100, conf=0.52)])
    (t,) = out.tracks
    assert t.state is TrackState.CONFIRMED and t.observed
    assert t.detection_confidence == pytest.approx(0.52)


# ---------------------------------------------------------------------------- occlusion
def confirmed_run(**cfg: Any) -> Run:
    run = Run(**cfg)
    for i in range(4):
        run.step([det(100 + 5 * i, 100)])
    return run


def test_short_detector_failure_marks_lost_then_recovers_with_same_id() -> None:
    run = confirmed_run()
    out = run.step([])
    assert states(out) == {1: TrackState.LOST}
    (t,) = out.tracks
    assert not t.observed and t.detection_confidence is None  # never invented
    assert t.missed_updates == 1 and t.last_observed_confidence == pytest.approx(0.9)
    out = run.step([])
    assert out.tracks[0].missed_updates == 2
    out = run.step([det(130, 100)])
    assert states(out) == {1: TrackState.CONFIRMED}
    assert out.tracks[0].observed and out.counts.recovered_total == 1
    assert out.counts.created_total == 1  # no new track was born


def test_long_disappearance_removes_the_track_and_a_return_gets_a_new_id() -> None:
    run = confirmed_run(max_missing_seconds=1.0)
    for _ in range(8):  # 8 * 120 ms = 960 ms: still within the limit
        out = run.step([])
    assert states(out) == {1: TrackState.LOST}
    out = run.step([])  # > 1.0 s since last seen
    assert out.tracks == [] and out.counts.removed_total == 1
    assert any(e.kind == "removed" and e.track_id == 1 for e in out.events)
    for _ in range(3):
        out = run.step([det(100, 100)])
    assert states(out) == {2: TrackState.CONFIRMED}  # a new, different id
    assert out.counts.created_total == 2


def test_expiry_is_time_based_not_frame_count_based() -> None:
    fast = confirmed_run(max_missing_seconds=1.0)
    slow = confirmed_run(max_missing_seconds=1.0)
    # Same number of missed updates, different elapsed time.
    for _ in range(3):
        f = fast.step([], dt_ms=100)
    for _ in range(3):
        s = slow.step([], dt_ms=600)
    assert states(f) == {1: TrackState.LOST}
    assert s.tracks == []  # 1.8 s without a detection


def test_tentative_track_is_rejected_after_its_timeout() -> None:
    run = Run(tentative_timeout_seconds=0.3, confirm_hits=3)
    run.step([det(100, 100)])
    run.step([])  # 120 ms: still tentative
    assert run.last is not None and run.last.counts.tentative == 1
    for _ in range(3):
        out = run.step([])
    assert out.tracks == [] and out.counts.rejected_total == 1
    assert out.counts.confirmed_total == 0 and out.counts.removed_total == 0


def test_face_cut_by_the_frame_edge_keeps_its_id_while_partly_visible() -> None:
    run = Run()
    x = 800.0
    for _ in range(5):
        run.step([det(x, 100)])
        x += 25
    # The detector clips boxes at the frame edge, so the visible part gets narrower.
    for width in (60, 45, 30):
        out = run.step([FaceDetection(1000 - width, 100, 1000, 200, 0.8)])
    assert [t.track_id for t in out.tracks] == [1]
    assert out.tracks[0].observed


def test_face_that_fully_leaves_and_returns_elsewhere_gets_a_new_id() -> None:
    """Documented limitation: no appearance model, so a long absence = a new track."""
    run = Run(max_missing_seconds=0.5)
    for _ in range(4):
        run.step([det(900, 100)])
    for _ in range(6):
        run.step([])
    for _ in range(3):
        out = run.step([det(100, 300)])
    assert [t.track_id for t in out.tracks] == [2]


# ---------------------------------------------------------------------------- crossing
def test_two_faces_crossing_keep_their_ids_with_clear_motion() -> None:
    run = Run()
    ids_at_start: dict[str, int] = {}
    for i in range(25):
        a = det(100 + 30 * i, 100)  # left -> right
        b = det(850 - 30 * i, 330)  # right -> left, vertically separated
        out = run.step([a, b])
        if i == 3:
            ids_at_start = {
                "a": next(t.track_id for t in out.tracks if t.bbox[0] < 500),
                "b": next(t.track_id for t in out.tracks if t.bbox[0] >= 500),
            }
    by_x = sorted(out.tracks, key=lambda t: t.bbox[0])
    assert by_x[0].track_id == ids_at_start["b"]  # b ended on the left
    assert by_x[1].track_id == ids_at_start["a"]  # a ended on the right
    assert out.counts.created_total == 2


def test_occlusion_by_another_face_recovers_the_hidden_one() -> None:
    run = Run()
    for i in range(5):
        run.step([det(100 + 10 * i, 100), det(500, 300)])
    first, second = (t.track_id for t in sorted(run.last.tracks, key=lambda t: t.bbox[0]))  # type: ignore[union-attr]
    for _ in range(3):  # first face hidden behind a person; only the second one is detected
        out = run.step([det(500, 300)])
    assert states(out)[first] is TrackState.LOST and states(out)[second] is TrackState.CONFIRMED
    out = run.step([det(165, 100), det(500, 300)])
    assert states(out) == {first: TrackState.CONFIRMED, second: TrackState.CONFIRMED}
    assert out.counts.created_total == 2


# ---------------------------------------------------------------------------- timing
def test_frame_skip_changes_dt_not_behaviour() -> None:
    """The same physical motion tracked at 120 ms or 480 ms steps yields one stable track."""
    for step_ms in (120, 480):
        run = Run()
        for i in range(6):
            x = 100 + 100 * (i * step_ms / 1000)  # 100 px/s
            out = run.step([det(x, 100)], dt_ms=step_ms)
        assert [t.track_id for t in out.tracks] == [1], step_ms
        assert out.tracks[0].state is TrackState.CONFIRMED


def test_nonuniform_timestamps_are_handled() -> None:
    run = Run()
    for dt in (120, 80, 500, 40, 200, 120):
        out = run.step([det(100, 100)], dt_ms=dt)
    assert [t.track_id for t in out.tracks] == [1]
    assert out.tracks[0].age_ms == 120 + 80 + 500 + 40 + 200  # real elapsed time, not frame counts


def test_velocity_estimate_predicts_motion_into_a_gap() -> None:
    run = Run()
    for i in range(6):
        run.step([det(100 + 20 * i, 100)])  # 20 px per 120 ms
    out = run.step([])
    x1 = out.tracks[0].bbox[0]
    assert 200 < x1 < 240  # continued moving right (last seen at 200)


def test_backwards_timestamp_is_counted_not_fatal() -> None:
    tracker = FaceByteTracker()
    tracker.update([det(10, 10)], 1000, 0, SIZE)
    out = tracker.update([det(10, 10)], 900, 3, SIZE)
    assert out.counts.timestamp_anomalies == 1
    assert [t.track_id for t in out.tracks] == [1]


# ---------------------------------------------------------------------------- robustness
@pytest.mark.parametrize(
    "bad",
    [
        FaceDetection(10, 10, 10, 50, 0.9),  # zero width
        FaceDetection(50, 10, 20, 50, 0.9),  # inverted
        FaceDetection(math.nan, 10, 20, 50, 0.9),
        FaceDetection(10, 10, math.inf, 50, 0.9),
        FaceDetection(10, 10, 20, 50, math.nan),
    ],
)
def test_invalid_boxes_are_ignored_and_counted(bad: FaceDetection) -> None:
    run = Run()
    out = run.step([bad, det(100, 100)])
    assert [t.track_id for t in out.tracks] == [1]
    assert out.counts.invalid_detections == 1


def test_track_ids_are_unique_and_never_reused() -> None:
    run = Run(max_missing_seconds=0.3)
    seen: list[int] = []
    for burst in range(5):
        for _ in range(3):
            out = run.step([det(100 + 150 * burst, 100)])
        seen.append(out.tracks[0].track_id)
        for _ in range(5):
            run.step([])
    assert len(set(seen)) == 5 and seen == sorted(seen)


def test_max_tracks_caps_births() -> None:
    run = Run(max_tracks=2)
    out = run.step([det(10, 10), det(200, 10), det(400, 10)])
    assert len(out.tracks) == 2 and out.counts.births_refused == 1


def test_iou_and_greedy_matching_are_deterministic() -> None:
    a = [(0.0, 0.0, 10.0, 10.0), (20.0, 0.0, 30.0, 10.0)]
    b = [(21.0, 0.0, 31.0, 10.0), (1.0, 0.0, 11.0, 10.0)]
    m = iou_matrix(a, b)
    assert m[0, 1] > 0.8 and m[1, 0] > 0.8 and m[0, 0] == 0
    assert greedy_match(m, 0.2) == [(0, 1), (1, 0)] or greedy_match(m, 0.2) == [(1, 0), (0, 1)]
    assert iou_matrix([], b).shape == (0, 2)
    assert greedy_match(m, 0.99) == []


def test_boxes_are_clipped_and_offscreen_predictions_are_not_emitted() -> None:
    run = Run()
    for i in range(5):
        run.step([det(700 + 60 * i, 100)])  # fast to the right
    for _ in range(12):
        out = run.step([])
        for t in out.tracks:
            assert 0 <= t.bbox[0] < t.bbox[2] <= SIZE[0]


def test_reset_clears_everything_and_restarts_ids() -> None:
    run = confirmed_run()
    run.tracker.reset()
    run.ts = run.frame = 0
    out = run.step([det(100, 100)])
    assert [t.track_id for t in out.tracks] == [1]
    assert out.counts.created_total == 1 and out.counts.confirmed_total == 0


def test_sessions_are_isolated() -> None:
    a, b = Run(), Run()
    for _ in range(4):
        a.step([det(100, 100)])
    out = b.step([det(500, 300)])
    assert out.counts.created_total == 1
    assert out.tracks[0].track_id == 1 and out.tracks[0].bbox[0] == 500


def test_events_describe_the_lifecycle_in_order() -> None:
    run = Run(confirm_hits=2, max_missing_seconds=0.2)
    kinds: list[str] = []
    for dets in ([det(100, 100)], [det(100, 100)], [], [det(100, 100)], [], [], []):
        kinds += [e.kind for e in run.step(dets).events]
    assert kinds == ["created", "confirmed", "lost", "recovered", "lost", "removed"]


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        TrackerConfig(confirm_hits=0)
    with pytest.raises(ValueError):
        TrackerConfig(max_missing_seconds=0)
    with pytest.raises(ValueError):
        TrackerConfig(match_iou=0)


def test_kalman_filter_tracks_constant_velocity() -> None:
    kf = BoxFilter((0, 0, 10, 10))
    for i in range(1, 11):
        kf.predict(0.1)
        kf.update((i, 0, 10 + i, 10))  # 10 px/s to the right
    kf.predict(0.1)
    assert kf.box()[0] == pytest.approx(11, abs=0.6)
    assert kf.axes[0].v == pytest.approx(10, abs=2)


def test_many_updates_do_not_leak_tracks() -> None:
    run = Run(max_missing_seconds=0.5)
    for i in range(300):
        dets = [det(50 + (i * 7) % 800, 100)] if i % 40 < 6 else []
        run.step(dets)
    assert run.tracker.counts().lost + run.tracker.counts().confirmed <= 2
    assert len(run.tracker._tracks) <= 2


def test_lost_track_is_reacquired_with_a_lower_gate_than_live_tracks() -> None:
    # Predicted box and the new detection overlap only ~0.14: below match_iou (0.2) but above the
    # LOST gate (0.1), so the id survives a gap; a live track would not have accepted it.
    run = Run(match_iou=0.2, lost_match_iou=0.1, lost_velocity_decay_per_second=20, box_buffer=0.0)
    for _ in range(4):
        run.step([det(100, 100)])
    run.step([])
    out = run.step([det(100 + 60, 100)])
    assert [t.track_id for t in out.tracks] == [1] and out.counts.recovered_total == 1
    strict = Run(
        match_iou=0.2, lost_match_iou=0.2, lost_velocity_decay_per_second=20, box_buffer=0.0
    )
    for _ in range(4):
        strict.step([det(100, 100)])
    strict.step([])
    out = strict.step([det(100 + 60, 100)])
    assert out.counts.recovered_total == 0


def test_unobserved_tentative_tracks_are_not_published() -> None:
    run = Run(confirm_hits=3, tentative_timeout_seconds=1.0)
    run.step([det(100, 100)])
    out = run.step([])  # still a live tentative track internally, but nothing to show
    assert out.tracks == [] and out.counts.tentative == 1
    out = run.step([det(100, 100)])
    assert [t.state for t in out.tracks] == [TrackState.TENTATIVE] and out.tracks[0].observed


def test_buffered_iou_keeps_tiny_jittery_faces_together() -> None:
    def tiny(x: float) -> FaceDetection:
        return FaceDetection(x, 100, x + 12, 114, 0.9)

    plain = Run(box_buffer=0.0)
    buffered = Run(box_buffer=0.2)
    for run in (plain, buffered):
        for i in range(6):
            run.step([tiny(100 + (6 if i % 2 else 0))])  # 6 px jitter on a 12 px face
    assert buffered.tracker.counts().created_total == 1
    assert plain.tracker.counts().created_total > 1


def test_lifecycle_events_are_logged_structurally_without_per_frame_info_noise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    run = Run(confirm_hits=2, max_missing_seconds=0.2)
    with caplog.at_level(logging.DEBUG, logger="katomaran.tracking"):
        for dets in ([det(100, 100)], [det(100, 100)], [det(101, 100)], [], [], [], []):
            run.step(dets)
    info = [
        (r.getMessage(), r.__dict__["track_id"])
        for r in caplog.records
        if r.levelno == logging.INFO
    ]
    assert info == [("track.confirmed", 1), ("track.lost", 1), ("track.removed", 1)]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == ["track.created"]  # 7 updates, but nothing is logged per frame
