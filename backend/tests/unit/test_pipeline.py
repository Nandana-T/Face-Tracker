"""Pipeline behaviour with deterministic fakes (no video files, no model)."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import pytest

from app.schemas.detection import DetectionResult
from app.vision.detection.interfaces import (
    DetectorError,
    FaceDetection,
    InvalidFrame,
)
from app.vision.sources.interfaces import Frame, SourceInfo, SourceUnavailable
from app.workers.pipeline import Outcome, PipelineSettings, ProcessingPipeline, should_detect

W, H = 64, 48


class FakeSource:
    def __init__(
        self, n: int, live: bool = False, fail_with: Exception | None = None, fps: float = 25.0
    ) -> None:
        self.n = n
        self.is_live = live
        self.fail_with = fail_with
        self.fps = fps
        self.opened = False
        self.closed = threading.Event()
        self.close_calls = 0
        self.corrupt_frames = 0

    def open(self) -> SourceInfo:
        self.opened = True
        return SourceInfo(W, H, self.fps, None if self.is_live else self.n, self.is_live)

    def frames(self) -> Iterator[Frame]:
        for i in range(self.n):
            if self.closed.is_set():
                return
            yield Frame(i, int(i * 1000 / self.fps), np.zeros((H, W, 3), dtype=np.uint8))
        if self.fail_with is not None:
            raise self.fail_with

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


class FakeDetector:
    name = "fake"

    def __init__(self, faces_for: dict[int, list[FaceDetection]] | None = None, delay: float = 0):
        self.faces_for = faces_for or {}
        self.delay = delay
        self.seen: list[int] = []
        self.closed = False
        self.last_inference_ms = 1.5
        self.raise_on: dict[int, Exception] = {}

    def detect(self, frame: Frame) -> list[FaceDetection]:
        if frame.index in self.raise_on:
            raise self.raise_on[frame.index]
        self.seen.append(frame.index)
        if self.delay:
            time.sleep(self.delay)
        return self.faces_for.get(frame.index, [])

    def close(self) -> None:
        self.closed = True


def run(
    source: FakeSource,
    detector: FakeDetector,
    on_result: Callable[[DetectionResult, bytes | None], None] | None = None,
    **settings: Any,
) -> tuple[ProcessingPipeline, Outcome, list[tuple[DetectionResult, bytes | None]], list[str]]:
    results: list[tuple[DetectionResult, bytes | None]] = []
    states: list[str] = []
    cb = on_result or (lambda r, j: results.append((r, j)))
    base: dict[str, Any] = {"realtime_pacing": False, "stream_max_fps": 1000.0, "frame_skip": 0}
    pipe = ProcessingPipeline(
        source,
        detector,
        PipelineSettings(**{**base, **settings}),
        on_result=cb,
        on_state=lambda s, d: states.append(s),
    )
    outcome = pipe.run()
    return pipe, outcome, results, states


# ---- frame skipping --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("skip", "expected"),
    [(0, [0, 1, 2, 3, 4, 5]), (1, [0, 2, 4]), (2, [0, 3]), (5, [0]), (100, [0])],
)
def test_should_detect_convention(skip: int, expected: list[int]) -> None:
    assert [i for i in range(6) if should_detect(i, skip)] == expected


def test_frame_skip_two_means_every_third_frame() -> None:
    det = FakeDetector()
    pipe, outcome, results, _ = run(FakeSource(10), det, frame_skip=2)
    assert outcome.state == "COMPLETED"
    assert det.seen == [0, 3, 6, 9]
    s = pipe.stats
    assert (s.frames_read, s.frames_processed, s.frames_skipped) == (10, 4, 6)
    assert [r.frame_id for r, _ in results] == [0, 3, 6, 9]


def test_skipped_frames_never_inherit_previous_detections() -> None:
    """Skipping must not be presented as tracking: no result is emitted for skipped frames."""
    face = FaceDetection(10, 10, 30, 30, 0.9)
    det = FakeDetector({0: [face]})
    _, _, results, _ = run(FakeSource(7), det, frame_skip=2)
    by_id = {r.frame_id: r for r, _ in results}
    assert len(by_id[0].faces) == 1
    assert by_id[3].faces == [] and by_id[6].faces == []
    assert set(by_id) == {0, 3, 6}


# ---- detection metadata ----------------------------------------------------------------------
def test_metadata_follows_the_documented_schema() -> None:
    faces = [FaceDetection(10.4, 5.6, 30.2, 25.9, 0.93456), FaceDetection(-5, -5, 20, 20, 0.5)]
    _, _, results, _ = run(FakeSource(1, fps=10), FakeDetector({0: faces}))
    r, jpeg = results[0]
    assert (r.frame_id, r.timestamp_ms, r.frame_width, r.frame_height) == (0, 0, W, H)
    assert r.faces[0].bbox == (10, 6, 30, 26)  # integer pixels, original-frame space
    assert r.faces[0].confidence == 0.9346
    assert r.faces[1].bbox == (0, 0, 20, 20)  # clipped to the frame
    assert jpeg is not None
    assert jpeg.startswith(b"\xff\xd8")


def test_degenerate_boxes_are_dropped_and_no_face_frames_allowed() -> None:
    face = FaceDetection(50, 40, 50.2, 40.2, 0.9)  # rounds to zero area
    _, _, results, _ = run(FakeSource(2), FakeDetector({0: [face]}))
    assert all(r.faces == [] for r, _ in results)


def test_timestamps_follow_fps() -> None:
    _, _, results, _ = run(FakeSource(6, fps=10), FakeDetector())
    assert [r.timestamp_ms for r, _ in results] == [0, 100, 200, 300, 400, 500]


def test_stream_rate_is_throttled_independently_of_detection() -> None:
    _, _, results, _ = run(FakeSource(30), FakeDetector(), stream_max_fps=0.001)
    assert len(results) == 30  # every detection is still reported ...
    assert sum(j is not None for _, j in results) == 1  # ... but only one JPEG was encoded


# ---- stats / lifecycle -----------------------------------------------------------------------
def test_stats_and_states() -> None:
    face = FaceDetection(1, 1, 20, 20, 0.9)
    det = FakeDetector({0: [face, face], 1: [face], 2: []})
    pipe, outcome, _, states = run(FakeSource(4), det)
    s = pipe.stats
    assert outcome.state == "COMPLETED"
    assert states == ["running"]
    assert (s.detections_total, s.max_faces_in_frame, s.faces_last_frame) == (3, 2, 0)
    snap = s.snapshot()
    assert snap["frames_read"] == 4 and snap["avg_inference_ms"] == 1.5
    assert snap["frame_width"] == W and snap["source_fps"] == 25.0


def test_resources_released_after_completion() -> None:
    src, det = FakeSource(5), FakeDetector()
    pipe, _, _, _ = run(src, det)
    assert src.closed.is_set() and det.closed and pipe.reader_joined


def test_stop_request_ends_session_and_cleans_up() -> None:
    src, det = FakeSource(10_000), FakeDetector(delay=0.002)
    pipe = ProcessingPipeline(
        src,
        det,
        PipelineSettings(realtime_pacing=False),
        on_result=lambda r, j: None,
    )
    result: list[object] = []
    t = threading.Thread(target=lambda: result.append(pipe.run()))
    t.start()
    time.sleep(0.15)
    pipe.request_stop()
    t.join(timeout=5)
    assert not t.is_alive()
    assert result[0].state == "STOPPED"  # type: ignore[attr-defined]
    assert pipe.stats.frames_read < 10_000
    assert src.closed.is_set() and det.closed and pipe.reader_joined


def test_stop_before_start_is_safe() -> None:
    src, det = FakeSource(50), FakeDetector()
    pipe = ProcessingPipeline(
        src,
        det,
        PipelineSettings(realtime_pacing=False),
        on_result=lambda r, j: None,
    )
    pipe.request_stop()
    pipe.request_stop()  # repeated stop must not raise
    assert pipe.run().state in ("STOPPED", "COMPLETED")
    assert det.closed


# ---- back-pressure ---------------------------------------------------------------------------
def test_live_source_drops_oldest_frames_when_inference_is_slow() -> None:
    src = FakeSource(300, live=True)
    det = FakeDetector(delay=0.01)
    pipe, outcome, _, _ = run(src, det, queue_size=2)
    s = pipe.stats
    assert outcome.state == "COMPLETED"
    assert s.frames_dropped > 0
    assert s.frames_processed + s.frames_skipped + s.frames_dropped == s.frames_read
    assert s.frames_processed < s.frames_read


def test_file_source_applies_backpressure_instead_of_dropping() -> None:
    src = FakeSource(60)
    pipe, _, _, _ = run(src, FakeDetector(delay=0.002), queue_size=2)
    s = pipe.stats
    assert s.frames_dropped == 0
    assert s.frames_processed == 60


def test_queue_is_bounded() -> None:
    src = FakeSource(500, live=True)
    pipe = ProcessingPipeline(
        src,
        FakeDetector(delay=0.005),
        PipelineSettings(realtime_pacing=False, queue_size=3),
        on_result=lambda r, j: None,
    )
    peak = 0
    t = threading.Thread(target=pipe.run)
    t.start()
    while t.is_alive():
        peak = max(peak, pipe._queue.qsize())
        time.sleep(0.001)
    assert 0 < peak <= 3
    assert pipe._queue.maxsize == 3


# ---- failures --------------------------------------------------------------------------------
def test_source_failure_becomes_error_outcome() -> None:
    src = FakeSource(3, fail_with=SourceUnavailable("camera gone", "camera_unavailable"))
    pipe, outcome, results, _ = run(src, FakeDetector())
    assert (outcome.state, outcome.code) == ("ERROR", "camera_unavailable")
    assert len(results) == 3  # frames before the failure were processed
    assert src.closed.is_set() and pipe.reader_joined


def test_open_failure_is_reported_and_cleaned() -> None:
    class Broken(FakeSource):
        def open(self) -> SourceInfo:
            raise SourceUnavailable("nope", "camera_unavailable")

    det = FakeDetector()
    _, outcome, _, _ = run(Broken(1), det)
    assert outcome.code == "camera_unavailable"
    assert det.closed  # the detector is released even though processing never began


def test_detector_error_is_fatal_but_invalid_frame_is_not() -> None:
    det = FakeDetector()
    det.raise_on = {2: InvalidFrame("bad frame")}
    pipe, outcome, _, _ = run(FakeSource(5), det)
    assert outcome.state == "COMPLETED"
    assert pipe.stats.corrupt_frames == 1
    assert pipe.stats.frames_processed == 4

    det2 = FakeDetector()
    det2.raise_on = {1: DetectorError("inference blew up", "inference_failed")}
    pipe2, outcome2, _, _ = run(FakeSource(5), det2)
    assert (outcome2.state, outcome2.code) == ("ERROR", "inference_failed")
    assert det2.closed and pipe2.reader_joined


def test_callback_crash_is_contained() -> None:
    def boom(_r: DetectionResult, _j: bytes | None) -> None:
        raise RuntimeError("publisher exploded")

    src, det = FakeSource(10), FakeDetector()
    pipe, outcome, _, _ = run(src, det, on_result=boom)
    assert (outcome.state, outcome.code) == ("ERROR", "processing_error")
    assert src.closed.is_set() and det.closed and pipe.reader_joined


def test_realtime_pacing_matches_source_rate() -> None:
    src = FakeSource(10, fps=50.0)  # 10 frames at 50 fps -> ~0.18 s
    started = time.perf_counter()
    run(src, FakeDetector(), realtime_pacing=True)
    elapsed = time.perf_counter() - started
    assert 0.15 <= elapsed < 1.5
