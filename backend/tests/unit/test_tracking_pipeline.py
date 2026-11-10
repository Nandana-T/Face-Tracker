"""Tracker + pipeline integration with deterministic fakes (no model, no video)."""

from __future__ import annotations

from typing import Any

from app.schemas.detection import DetectionResult
from app.vision.detection.interfaces import FaceDetection
from app.vision.tracking.interfaces import TrackerOutput
from app.vision.tracking.tracker import FaceByteTracker, TrackerConfig
from app.workers.pipeline import PipelineSettings, ProcessingPipeline
from tests.unit.test_pipeline import FakeDetector, FakeSource

FACE = FaceDetection(10, 10, 30, 34, 0.9)


def run_with_tracker(
    n: int,
    faces_for: dict[int, list[FaceDetection]],
    tracker: Any,
    frame_skip: int = 2,
    fps: float = 25.0,
) -> tuple[ProcessingPipeline, list[DetectionResult]]:
    results: list[DetectionResult] = []
    pipe = ProcessingPipeline(
        FakeSource(n, fps=fps),
        FakeDetector(faces_for),
        PipelineSettings(realtime_pacing=False, stream_max_fps=1000.0, frame_skip=frame_skip),
        on_result=lambda r, j: results.append(r),
        tracker=tracker,
    )
    assert pipe.run().state == "COMPLETED"
    return pipe, results


def test_detections_feed_the_tracker_and_tracks_are_published() -> None:
    faces = {i: [FACE] for i in range(0, 30, 3)}
    pipe, results = run_with_tracker(30, faces, FaceByteTracker())
    assert [r.frame_id for r in results] == list(range(0, 30, 3))
    last = results[-1]
    assert len(last.faces) == 1  # raw detections stay available (Phase 1 contract)
    (track,) = last.tracks
    assert track.track_id == 1 and track.state == "CONFIRMED" and track.observed
    assert track.detection_confidence == 0.9
    assert last.tracking is not None and last.tracking.confirmed == 1
    assert results[0].tracks[0].state == "TENTATIVE"
    snap = pipe.stats.snapshot()
    assert snap["tracks_confirmed"] == 1 and snap["tracks_created_total"] == 1
    assert snap["tracking_ms_avg"] >= 0


def test_tracker_is_updated_only_on_analysed_frames_with_real_timestamps() -> None:
    calls: list[tuple[int, int]] = []

    class Spy(FaceByteTracker):
        def update(self, detections, timestamp_ms, frame_id, frame_size=None) -> TrackerOutput:  # type: ignore[no-untyped-def]
            calls.append((frame_id, timestamp_ms))
            return super().update(detections, timestamp_ms, frame_id, frame_size)

    run_with_tracker(13, {}, Spy(), frame_skip=2, fps=25.0)
    assert calls == [(0, 0), (3, 120), (6, 240), (9, 360), (12, 480)]


def test_skipped_frames_publish_no_stale_boxes() -> None:
    faces = {i: [FACE] for i in range(0, 9, 3)}
    _, results = run_with_tracker(21, faces, FaceByteTracker(), frame_skip=2)
    assert {r.frame_id for r in results} == {0, 3, 6, 9, 12, 15, 18}
    # After the only detection, later frames show a PREDICTED track without a confidence.
    later = [t for r in results[3:] for t in r.tracks]
    assert later and all(not t.observed and t.detection_confidence is None for t in later)
    assert all(t.last_observed_confidence == 0.9 for t in later)


def test_lost_tracks_are_reported_as_predicted_never_observed() -> None:
    faces = {i: [FACE] for i in range(0, 12, 3)}  # 4 detections, then nothing
    _, results = run_with_tracker(24, faces, FaceByteTracker(), frame_skip=2)
    final = results[-1]
    assert final.tracks and final.tracks[0].state == "LOST"
    assert final.tracks[0].observed is False


def test_tracking_disabled_keeps_phase_one_payload() -> None:
    pipe, results = run_with_tracker(9, {0: [FACE]}, None)
    assert all(r.tracks == [] and r.tracking is None for r in results)
    assert "tracks_confirmed" not in pipe.stats.snapshot()


def test_tracker_failure_does_not_stop_detection() -> None:
    class Broken(FaceByteTracker):
        def update(self, *a, **k):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    faces = {i: [FACE] for i in range(0, 12, 3)}
    pipe, results = run_with_tracker(12, faces, Broken())
    assert len(results) == 4 and all(len(r.faces) == 1 for r in results)
    assert all(r.tracks == [] for r in results)
    assert pipe.stats.tracking_errors == 4
    assert pipe.stats.snapshot()["tracks_errors"] == 4


def test_tracker_is_reset_when_the_session_ends() -> None:
    tracker = FaceByteTracker()
    run_with_tracker(12, {i: [FACE] for i in range(0, 12, 3)}, tracker)
    assert tracker.counts().created_total == 0  # reset on cleanup
    assert tracker._tracks == []


def test_two_sessions_do_not_share_track_ids() -> None:
    for _ in range(2):
        _, results = run_with_tracker(
            12, {i: [FACE] for i in range(0, 12, 3)}, FaceByteTracker(TrackerConfig())
        )
        assert [t.track_id for t in results[-1].tracks] == [1]


def test_track_boxes_stay_inside_the_frame() -> None:
    edge = FaceDetection(40, 10, 64, 40, 0.9)  # frame is 64x48
    _, results = run_with_tracker(30, {i: [edge] for i in range(0, 30, 3)}, FaceByteTracker())
    for r in results:
        for t in r.tracks:
            assert 0 <= t.bbox[0] < t.bbox[2] <= r.frame_width
            assert 0 <= t.bbox[1] < t.bbox[3] <= r.frame_height
