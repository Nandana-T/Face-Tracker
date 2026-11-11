"""Worker-side recognition: schedule which tracks to embed, and do it off the inference thread.

Crop -> landmarks -> alignment -> quality gate -> embedding. Runs on its own thread behind a
bounded queue (``RecognitionRunner``): when it is busy new jobs are dropped (and counted), so
recognition can never slow down or block detection/tracking. A failed quality gate or a model error
only affects that observation.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.core.config import RecognitionConfig
from app.vision.recognition import registry as R
from app.vision.recognition.alignment import AlignmentError, align_face
from app.vision.recognition.embedder import EmbeddingError, OrtEmbedder
from app.vision.recognition.landmarks import YuNetLandmarks
from app.vision.recognition.quality import QualityConfig, assess

logger = logging.getLogger("katomaran.recognition")


def quality_config(cfg: RecognitionConfig) -> QualityConfig:
    return QualityConfig(
        min_face_px=cfg.min_face_px,
        min_detection_confidence=cfg.min_detection_confidence,
        max_yaw=cfg.max_yaw,
        max_roll_degrees=cfg.max_roll_degrees,
        min_sharpness=cfg.min_sharpness,
    )


@dataclass(frozen=True)
class FaceJob:
    track_id: int
    seq: int
    crop: np.ndarray  # padded BGR crop around the face (a copy; never the full frame)
    box_in_crop: tuple[float, float, float, float]
    frame_box: tuple[float, float, float, float]
    frame_size: tuple[int, int]
    detection_confidence: float
    timestamp_ms: int
    frame_id: int


@dataclass(frozen=True)
class FaceResult:
    track_id: int
    seq: int
    embedding: np.ndarray | None  # unit vector; None = quality gate / alignment / model failure
    quality: float
    reasons: tuple[str, ...]
    timestamp_ms: int
    frame_id: int
    crop_jpeg: bytes | None
    process_ms: float


def make_job(
    pixels: np.ndarray,
    box: tuple[float, float, float, float],
    track_id: int,
    seq: int,
    detection_confidence: float,
    timestamp_ms: int,
    frame_id: int,
    pad: float,
) -> FaceJob:
    h, w = pixels.shape[:2]
    x1, y1, x2, y2 = box
    px, py = (x2 - x1) * pad, (y2 - y1) * pad
    cx1, cy1 = max(0, int(x1 - px)), max(0, int(y1 - py))
    cx2, cy2 = min(w, int(x2 + px + 1)), min(h, int(y2 + py + 1))
    crop = pixels[cy1:cy2, cx1:cx2].copy()
    return FaceJob(
        track_id, seq, crop, (x1 - cx1, y1 - cy1, x2 - cx1, y2 - cy1), box, (w, h),
        detection_confidence, timestamp_ms, frame_id,
    )  # fmt: skip


class FaceRecognizer:
    def __init__(self, models_dir: Path, cfg: RecognitionConfig) -> None:
        self.cfg = cfg
        self._quality = quality_config(cfg)
        started = time.perf_counter()
        spec = R.SFACE
        path = (
            Path(cfg.model_path)
            if cfg.model_path and Path(cfg.model_path).is_absolute()
            else (models_dir / cfg.model_path if cfg.model_path else R.model_path(models_dir, spec))
        )
        if not cfg.model_path:
            R.verify_model_file(path, spec)
            R.verify_model_file(R.model_path(models_dir, R.YUNET), R.YUNET)
        self.embedder = OrtEmbedder(path, spec, threads=2)
        self.landmarks = YuNetLandmarks(R.model_path(models_dir, R.YUNET), pad=cfg.landmark_pad)
        self.model_version = self.embedder.model_version
        self.dim = self.embedder.dim
        self.load_seconds = time.perf_counter() - started

    def process(self, job: FaceJob) -> FaceResult:
        started = time.perf_counter()
        crop_jpeg = _encode_crop(job.crop, self.cfg.crop_max_px)
        landmarks = None
        aligned = None
        try:
            found = self.landmarks.locate(job.crop, job.box_in_crop)
            if found is not None:
                landmarks = found.points
                aligned = align_face(job.crop, found.points)
        except (AlignmentError, cv2.error):
            landmarks = aligned = None
        result = assess(
            self._quality,
            job.frame_box,
            job.detection_confidence,
            job.frame_size,
            landmarks,
            aligned,
        )
        embedding: np.ndarray | None = None
        reasons = result.reasons
        if result.ok and aligned is not None:
            try:
                embedding = self.embedder.embed(aligned)
            except EmbeddingError as exc:
                logger.warning("Embedding failed: %s", exc)
                reasons = (*reasons, "embedding_failed")
        return FaceResult(
            job.track_id, job.seq, embedding, result.score, reasons, job.timestamp_ms, job.frame_id,
            crop_jpeg if embedding is not None else None, (time.perf_counter() - started) * 1000,
        )  # fmt: skip


def _encode_crop(crop: np.ndarray, max_px: int) -> bytes | None:
    h, w = crop.shape[:2]
    if h < 8 or w < 8:
        return None
    scale = min(1.0, max_px / max(h, w))
    img = (
        cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1
        else crop
    )
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return bytes(buf.tobytes()) if ok else None


# --------------------------------------------------------------------------------- scheduling
@dataclass
class _TrackSchedule:
    last_ms: int = -(10**12)
    sent: int = 0
    seq: int = 0


class RecognitionScheduler:
    """Decides which tracks get an embedding at an analysed frame (never every face every frame)."""

    def __init__(self, cfg: RecognitionConfig) -> None:
        self.cfg = cfg
        self._tracks: dict[int, _TrackSchedule] = {}
        self.ema_ms = 40.0  # running estimate of the cost of one job (updated by the runner)

    def select(self, track_ids: list[int], now_ms: int) -> list[tuple[int, int]]:
        """Return ``[(track_id, seq)]`` to submit now, oldest-served first, within the time budget."""
        cfg = self.cfg
        # Schedules are kept for the whole session (track ids are never reused): forgetting a track
        # that is briefly not "observed" would restart its sequence numbers and the API would drop the
        # repeats as duplicate observation ids. Bounded: one tiny record per track ever seen.
        if len(self._tracks) > 5000:
            for old_id in sorted(self._tracks)[:1000]:
                del self._tracks[old_id]
        due: list[tuple[int, int]] = []
        for tid in track_ids:
            st = self._tracks.setdefault(tid, _TrackSchedule())
            interval = (
                cfg.observation_interval_seconds
                if st.sent < cfg.warmup_observations
                else cfg.refresh_interval_seconds
            )
            if now_ms - st.last_ms >= interval * 1000:
                due.append((st.last_ms, tid))
        due.sort()
        budget = max(1, int(cfg.budget_ms_per_frame / max(self.ema_ms, 1.0)))
        chosen: list[tuple[int, int]] = []
        for _, tid in due[:budget]:
            st = self._tracks[tid]
            st.last_ms = now_ms
            st.sent += 1
            st.seq += 1
            chosen.append((tid, st.seq))
        return chosen

    def note_cost(self, ms: float) -> None:
        self.ema_ms = 0.8 * self.ema_ms + 0.2 * ms


@dataclass
class RunnerStats:
    submitted: int = 0
    dropped: int = 0
    processed: int = 0
    usable: int = 0
    process_ms_total: float = 0.0
    errors: int = 0
    queue_peak: int = 0
    extra: dict[str, float] = field(default_factory=dict)

    def snapshot(self) -> dict[str, float | int]:
        n = max(1, self.processed)
        return {
            "recog_submitted": self.submitted,
            "recog_dropped": self.dropped,
            "recog_processed": self.processed,
            "recog_usable": self.usable,
            "recog_errors": self.errors,
            "recog_avg_ms": round(self.process_ms_total / n, 2),
            "recog_queue_peak": self.queue_peak,
        }


class RecognitionRunner:
    """One background thread, bounded queue, drop-newest when full."""

    def __init__(
        self,
        recognizer: FaceRecognizer,
        on_result: Callable[[FaceResult], None],
        scheduler: RecognitionScheduler,
        queue_size: int,
    ) -> None:
        self._recognizer = recognizer
        self._on_result = on_result
        self.scheduler = scheduler
        self._queue: queue.Queue[FaceJob | None] = queue.Queue(maxsize=queue_size)
        self.stats = RunnerStats()
        self._thread = threading.Thread(target=self._loop, name="recognition", daemon=True)
        self._stopped = False
        self._thread.start()

    def submit(self, job: FaceJob) -> bool:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self.stats.dropped += 1
            return False
        self.stats.submitted += 1
        self.stats.queue_peak = max(self.stats.queue_peak, self._queue.qsize())
        return True

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job is None:
                return
            try:
                result = self._recognizer.process(job)
            except Exception:  # noqa: BLE001 - one bad face must not end recognition
                self.stats.errors += 1
                logger.exception("Recognition job failed")
                continue
            self.stats.processed += 1
            self.stats.process_ms_total += result.process_ms
            self.scheduler.note_cost(result.process_ms)
            if result.embedding is not None:
                self.stats.usable += 1
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001
                self.stats.errors += 1
                logger.exception("Recognition result callback failed")

    def close(self, timeout: float = 3.0) -> None:
        if self._stopped:
            return
        self._stopped = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # drop pending work: the session is over
            try:
                while True:
                    self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(None)
        self._thread.join(timeout=timeout)
