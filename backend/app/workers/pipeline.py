"""Video processing pipeline: source -> (bounded queue) -> frame-skip -> detector -> publisher.

Runs on plain threads, completely independent of any asyncio loop / FastAPI.

Back-pressure policy
--------------------
* The reader thread and the inference thread are connected by a bounded queue.
* Live sources (RTSP): when the queue is full the OLDEST frame is dropped (latency beats
  completeness for a camera). Drops are counted as ``frames_dropped``.
* Files: the reader blocks until there is room (nothing is dropped; playback simply slows down
  when inference cannot keep up).
* Frames never accumulate beyond ``queue_size``.

Frame skipping: ``frame_skip = N`` runs the detector on frame indices 0, N+1, 2(N+1), ...
Skipped frames are counted and never published; detections are NOT carried over or interpolated.

Tracking (Phase 2): the tracker, when configured, is updated exactly once per ANALYSED frame with
the frame's real timestamp, so its time-based rules (confirmation, lost-time limits, velocity) stay
correct whatever ``frame_skip`` or the source FPS is. A tracker failure never stops detection: the
error is counted, the tracker is reset and processing continues.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import cv2

from app.schemas.detection import DetectionResult, FaceBox
from app.schemas.tracking import TrackingSummary, TrackOut
from app.vision.detection.interfaces import DetectorError, FaceDetector, InvalidFrame
from app.vision.sources.interfaces import Frame, SourceError, VideoSource
from app.vision.tracking.interfaces import FaceTracker, TrackerOutput

logger = logging.getLogger("katomaran.pipeline")

_END = object()


def should_detect(frame_index: int, frame_skip: int) -> bool:
    """True for frame indices 0, N+1, 2(N+1), ... where N = frame_skip."""
    return frame_index % (frame_skip + 1) == 0


@dataclass(frozen=True)
class PipelineSettings:
    frame_skip: int = 2
    queue_size: int = 4
    realtime_pacing: bool = True
    stream_max_fps: float = 15.0
    stream_max_width: int = 960
    jpeg_quality: int = 70
    stats_interval_seconds: float = 1.0


@dataclass
class Outcome:
    state: str  # COMPLETED | STOPPED | ERROR
    code: str | None = None
    message: str | None = None


@dataclass
class PipelineStats:
    frames_read: int = 0
    frames_processed: int = 0
    frames_skipped: int = 0
    frames_dropped: int = 0
    corrupt_frames: int = 0
    detections_total: int = 0  # sum of faces over processed frames; NOT unique people
    faces_last_frame: int = 0
    max_faces_in_frame: int = 0
    last_inference_ms: float = 0.0
    last_frame_id: int = -1
    frame_width: int = 0
    frame_height: int = 0
    source_fps: float = 0.0
    tracking: dict[str, Any] = field(default_factory=dict)  # flat tracks_* counters
    tracking_errors: int = 0
    _inference_total_ms: float = 0.0
    _tracking_total_ms: float = 0.0
    _started: float = field(default_factory=time.monotonic)
    _recent: deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=64))

    def record_processed(self, now: float) -> None:
        self._recent.append((now, self.frames_processed))

    def processing_fps(self) -> float:
        if len(self._recent) < 2:
            return 0.0
        (t0, n0), (t1, n1) = self._recent[0], self._recent[-1]
        return round((n1 - n0) / (t1 - t0), 2) if t1 > t0 else 0.0

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.monotonic()
        avg = self._inference_total_ms / self.frames_processed if self.frames_processed else 0.0
        return {
            "frames_read": self.frames_read,
            "frames_processed": self.frames_processed,
            "frames_skipped": self.frames_skipped,
            "frames_dropped": self.frames_dropped,
            "corrupt_frames": self.corrupt_frames,
            "detections_total": self.detections_total,
            "faces_last_frame": self.faces_last_frame,
            "max_faces_in_frame": self.max_faces_in_frame,
            "last_inference_ms": round(self.last_inference_ms, 2),
            "avg_inference_ms": round(avg, 2),
            "processing_fps": self.processing_fps(),
            "last_frame_id": self.last_frame_id,
            "frame_width": self.frame_width,
            "frame_height": self.frame_height,
            "source_fps": round(self.source_fps, 2),
            "elapsed_seconds": round(now - self._started, 2),
            **self.tracking,
        }


ResultCallback = Callable[[DetectionResult, bytes | None], None]
StateCallback = Callable[[str, str | None], None]
StatsCallback = Callable[[dict[str, Any]], None]


def encode_jpeg(pixels: Any, max_width: int, quality: int) -> bytes:
    h, w = pixels.shape[:2]
    if w > max_width:
        scale = max_width / w
        pixels = cv2.resize(
            pixels, (max_width, max(1, round(h * scale))), interpolation=cv2.INTER_AREA
        )
    ok, buf = cv2.imencode(".jpg", pixels, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise DetectorError("JPEG encoding failed", "encode_failed")
    return bytes(buf.tobytes())


def to_result(
    frame: Frame,
    faces: list[Any],
    inference_ms: float,
    tracking: TrackerOutput | None = None,
    tracking_ms: float = 0.0,
) -> DetectionResult:
    boxes: list[FaceBox] = []
    for f in faces:
        x1, y1 = max(0, int(round(f.x1))), max(0, int(round(f.y1)))
        x2, y2 = min(frame.width, int(round(f.x2))), min(frame.height, int(round(f.y2)))
        if x2 > x1 and y2 > y1:
            boxes.append(FaceBox(bbox=(x1, y1, x2, y2), confidence=round(f.confidence, 4)))
    tracks: list[TrackOut] = []
    summary: TrackingSummary | None = None
    if tracking is not None:
        for t in tracking.tracks:
            bx1, by1 = max(0, int(round(t.bbox[0]))), max(0, int(round(t.bbox[1])))
            bx2 = min(frame.width, int(round(t.bbox[2])))
            by2 = min(frame.height, int(round(t.bbox[3])))
            if bx2 <= bx1 or by2 <= by1:
                continue
            conf = t.detection_confidence
            last = t.last_observed_confidence
            tracks.append(
                TrackOut(
                    track_id=t.track_id,
                    state=t.state.value,
                    bbox=(bx1, by1, bx2, by2),
                    observed=t.observed,
                    detection_confidence=None if conf is None else round(conf, 4),
                    last_observed_confidence=None if last is None else round(last, 4),
                    first_seen_ms=t.first_seen_ms,
                    last_seen_ms=t.last_seen_ms,
                    age_ms=t.age_ms,
                    hits=t.hits,
                    missed_updates=t.missed_updates,
                    last_update_frame=t.last_update_frame,
                )
            )
        c = tracking.counts
        summary = TrackingSummary(
            tentative=c.tentative,
            confirmed=c.confirmed,
            lost=c.lost,
            created_total=c.created_total,
            confirmed_total=c.confirmed_total,
            tracking_ms=round(tracking_ms, 3),
        )
    return DetectionResult(
        frame_id=frame.index,
        timestamp_ms=frame.timestamp_ms,
        frame_width=frame.width,
        frame_height=frame.height,
        faces=boxes,
        inference_ms=round(inference_ms, 2),
        tracks=tracks,
        tracking=summary,
    )


class ProcessingPipeline:
    def __init__(
        self,
        source: VideoSource,
        detector: FaceDetector,
        settings: PipelineSettings,
        on_result: ResultCallback,
        on_state: StateCallback | None = None,
        on_stats: StatsCallback | None = None,
        clock: Callable[[], float] = time.monotonic,
        tracker: FaceTracker | None = None,
    ) -> None:
        self.tracker = tracker
        self.source = source
        self.detector = detector
        self.settings = settings
        self._on_result = on_result
        self._on_state = on_state or (lambda _s, _d: None)
        self._on_stats = on_stats or (lambda _s: None)
        self._clock = clock
        self.stats = PipelineStats()
        self._stop = threading.Event()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=settings.queue_size)
        self._reader: threading.Thread | None = None
        self._reader_error: BaseException | None = None
        self._queue_lock = threading.Lock()
        self.reader_joined = False
        self._last_stats = 0.0

    # -- control --------------------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()
        # Unblocks a reader stuck in a (possibly long) network read.
        try:
            self.source.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("Error while closing source during stop")

    # -- reader thread --------------------------------------------------------------
    def _put(self, item: Any, live: bool) -> None:
        if live:
            while True:
                try:
                    self._queue.put_nowait(item)
                    return
                except queue.Full:
                    with self._queue_lock:
                        try:
                            self._queue.get_nowait()
                            self.stats.frames_dropped += 1
                        except queue.Empty:
                            pass
        while not self._stop.is_set():
            try:
                self._queue.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    def _read_loop(self, fps: float, live: bool) -> None:
        pacing = self.settings.realtime_pacing and not live and fps > 0
        t0 = self._clock()
        try:
            for frame in self.source.frames():
                if self._stop.is_set():
                    break
                if pacing:
                    delay = t0 + frame.index / fps - self._clock()
                    if delay > 0:
                        self._stop.wait(delay)
                self.stats.frames_read += 1
                self._put(frame, live)
        except BaseException as exc:  # noqa: BLE001 - handed to the consumer
            self._reader_error = exc
        finally:
            if not self._stop.is_set():
                self._put(_END, live=False)

    # -- main loop ------------------------------------------------------------------
    def run(self) -> Outcome:
        outcome = Outcome("ERROR", "processing_error", "Processing ended unexpectedly.")
        try:
            info = self.source.open()
            self.stats.frame_width, self.stats.frame_height = info.width, info.height
            self.stats.source_fps = info.fps
            logger.info(
                "Pipeline started",
                extra={
                    "live": info.is_live,
                    "fps": round(info.fps, 2),
                    "skip": self.settings.frame_skip,
                },
            )
            if self.tracker is not None:
                logger.info("Tracker initialized")
            self._reader = threading.Thread(
                target=self._read_loop,
                args=(info.fps, info.is_live),
                name="frame-reader",
                daemon=True,
            )
            self._reader.start()
            outcome = self._consume()
        except SourceError as exc:
            logger.warning("Source failure: %s", exc.code)
            outcome = Outcome("ERROR", exc.code, str(exc))
        except Exception:
            logger.exception("Pipeline crashed")
            outcome = Outcome("ERROR", "processing_error", "An internal processing error occurred.")
        finally:
            self._stop.set()
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing source")
            if self._reader is not None:
                self._reader.join(timeout=5)
                self.reader_joined = not self._reader.is_alive()
                if not self.reader_joined:
                    logger.error("Frame reader thread did not terminate within 5 s")
            if self.tracker is not None:
                try:
                    self.tracker.reset()
                    logger.info("Tracker reset (session ended)")
                except Exception:  # noqa: BLE001
                    logger.exception("Tracker reset failed")
            try:
                self.detector.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing detector")
            final = self._snapshot()
            self._on_stats(final)
            logger.info("Pipeline finished", extra={"state": outcome.state, **final})
        return outcome

    def _consume(self) -> Outcome:
        s = self.settings
        running_announced = False
        self._last_stats = self._clock()
        last_stream = -1e9
        min_gap = 1.0 / s.stream_max_fps
        while True:
            if self._stop.is_set():
                return Outcome("STOPPED")
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                self._tick_stats()
                continue
            if item is _END:
                if self._reader_error is not None:
                    err = self._reader_error
                    if isinstance(err, SourceError):
                        return Outcome("ERROR", err.code, str(err))
                    logger.error("Reader crashed", exc_info=err)
                    return Outcome("ERROR", "processing_error", "The video reader failed.")
                return Outcome("COMPLETED")
            frame: Frame = item
            if not running_announced:
                running_announced = True
                self._on_state("running", None)
            self.stats.last_frame_id = frame.index
            if not should_detect(frame.index, s.frame_skip):
                self.stats.frames_skipped += 1
            else:
                try:
                    started = self._clock()
                    faces = self.detector.detect(frame)
                    elapsed_ms = (self._clock() - started) * 1000
                except InvalidFrame:
                    self.stats.corrupt_frames += 1
                    continue
                except DetectorError as exc:
                    return Outcome("ERROR", exc.code, str(exc))
                inference_ms = getattr(self.detector, "last_inference_ms", None) or elapsed_ms
                track_out, tracking_ms = self._track(frame, faces)
                result = to_result(frame, faces, inference_ms, track_out, tracking_ms)
                st = self.stats
                st.frames_processed += 1
                st.faces_last_frame = len(result.faces)
                st.detections_total += len(result.faces)
                st.max_faces_in_frame = max(st.max_faces_in_frame, len(result.faces))
                st.last_inference_ms = inference_ms
                st._inference_total_ms += inference_ms
                st.record_processed(self._clock())
                now = self._clock()
                jpeg: bytes | None = None
                if now - last_stream >= min_gap:
                    last_stream = now
                    jpeg = encode_jpeg(frame.pixels, s.stream_max_width, s.jpeg_quality)
                self._on_result(result, jpeg)
            self._tick_stats()

    def _track(self, frame: Frame, faces: list[Any]) -> tuple[TrackerOutput | None, float]:
        """Update the tracker with this analysed frame. Never raises."""
        tracker = self.tracker
        if tracker is None:
            return None, 0.0
        try:
            started = time.perf_counter()
            out = tracker.update(
                list(faces), frame.timestamp_ms, frame.index, (frame.width, frame.height)
            )
            elapsed_ms = (time.perf_counter() - started) * 1000
        except Exception:  # noqa: BLE001 - tracking must never take detection down
            self.stats.tracking_errors += 1
            logger.exception("Tracker update failed; tracker reset")
            try:
                tracker.reset()
            except Exception:  # noqa: BLE001
                logger.exception("Tracker reset failed")
            st = self.stats
            st.tracking = {**st.tracking, "tracks_errors": st.tracking_errors}
            return None, 0.0
        self.stats._tracking_total_ms += elapsed_ms
        self._publish_tracking_stats(out, elapsed_ms)
        return out, elapsed_ms

    def _publish_tracking_stats(self, out: TrackerOutput, elapsed_ms: float) -> None:
        st = self.stats
        c = out.counts
        n = st.frames_processed + 1  # the current frame is counted right after this call
        st.tracking = {
            "tracks_enabled": 1,
            "tracks_confirmed": c.confirmed,
            "tracks_tentative": c.tentative,
            "tracks_lost": c.lost,
            "tracks_created_total": c.created_total,
            "tracks_confirmed_total": c.confirmed_total,
            "tracks_rejected_total": c.rejected_total,
            "tracks_removed_total": c.removed_total,
            "tracks_recovered_total": c.recovered_total,
            "tracks_invalid_detections": c.invalid_detections,
            "tracks_errors": st.tracking_errors,
            "tracking_ms_last": round(elapsed_ms, 3),
            "tracking_ms_avg": round(st._tracking_total_ms / n, 3),
        }

    def _tick_stats(self) -> None:
        now = self._clock()
        if now - self._last_stats >= self.settings.stats_interval_seconds:
            self._last_stats = now
            self._on_stats(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        snap = self.stats.snapshot()
        snap["corrupt_frames"] += int(getattr(self.source, "corrupt_frames", 0))
        return snap
