"""ByteTrack-style multi-face tracker (own implementation, no third-party tracking package).

Algorithm (per update, which happens only on frames where the detector ran):

1. ``dt`` = real elapsed source time since the previous update. Every live track is advanced to now
   with a constant-velocity Kalman filter (velocity of LOST tracks decays, so they do not fly off).
2. Detections are split by score: *high* (>= ``high_confidence``) and *low* (the rest, which already
   passed the detector's own threshold).
3. Association by IoU between predicted track boxes and detections (greedy, best IoU first, ties
   broken by track id then detection order - fully deterministic):
   a. CONFIRMED + LOST tracks  x  high detections        (``match_iou``)
   b. still-unmatched CONFIRMED/LOST tracks  x  low detections   (``low_match_iou``) - ByteTrack's
      idea: a weak detection may extend an existing track but may never create one
   c. TENTATIVE tracks  x  remaining high detections     (``match_iou``)
4. Unmatched high detections start TENTATIVE tracks. Only high-score detections can start tracks.
5. Lifecycle by *time*, not frame counts (see ``interfaces.py``).

Why not a single frame-count rule: with ``frame_skip`` the detector runs every N+1 frames and the
source FPS differs between files and cameras; seconds mean the same thing everywhere.

Limits (documented in docs/phase-2-report.md): motion + box overlap only. Two faces that cross while
overlapping, or a face hidden longer than ``max_missing_seconds``, can swap or lose their id;
there is no appearance model (recognition is a later phase and deliberately not used here).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np

from app.vision.detection.interfaces import FaceDetection
from app.vision.tracking.interfaces import (
    TrackerCounts,
    TrackerOutput,
    TrackEvent,
    TrackSnapshot,
    TrackState,
)
from app.vision.tracking.kalman import BoxFilter

logger = logging.getLogger("katomaran.tracking")

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class TrackerConfig:
    high_confidence: float = 0.6
    confirm_hits: int = 3
    tentative_timeout_seconds: float = 0.5
    max_missing_seconds: float = 2.0
    match_iou: float = 0.2
    low_match_iou: float = 0.3
    # Gate for re-acquiring a LOST track (its predicted box is less certain than a live one).
    lost_match_iou: float = 0.1
    max_tracks: int = 200
    # Association uses IoU of boxes enlarged by this fraction of their size on every side ("buffered
    # IoU"), which keeps very small, fast faces matchable. 0 = plain IoU.
    box_buffer: float = 0.1
    lost_velocity_decay_per_second: float = 1.0
    accel_scale: float = 4.0
    measurement_scale: float = 0.06

    def __post_init__(self) -> None:
        if self.confirm_hits < 1:
            raise ValueError("confirm_hits must be >= 1")
        if self.max_missing_seconds <= 0 or self.tentative_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if not (0 < self.match_iou <= 1 and 0 < self.low_match_iou <= 1):
            raise ValueError("IoU thresholds must be in (0, 1]")


class _Track:
    __slots__ = (
        "id",
        "state",
        "kf",
        "first_ms",
        "last_seen_ms",
        "hits",
        "missed",
        "last_conf",
        "cur_conf",
        "last_frame",
        "matched_now",
    )

    def __init__(
        self, track_id: int, det: FaceDetection, ts: int, frame_id: int, cfg: TrackerConfig
    ) -> None:
        self.id = track_id
        self.state = TrackState.TENTATIVE
        self.kf = BoxFilter(_box(det), cfg.accel_scale, cfg.measurement_scale)
        self.first_ms = ts
        self.last_seen_ms = ts
        self.hits = 1
        self.missed = 0
        self.last_conf: float | None = det.confidence
        self.cur_conf: float | None = det.confidence
        self.last_frame = frame_id
        self.matched_now = True


def _box(d: FaceDetection) -> Box:
    return (d.x1, d.y1, d.x2, d.y2)


def _grow(b: Box, fraction: float) -> Box:
    if fraction <= 0:
        return b
    dx, dy = (b[2] - b[0]) * fraction, (b[3] - b[1]) * fraction
    return (b[0] - dx, b[1] - dy, b[2] + dx, b[3] + dy)


def _valid(d: FaceDetection) -> bool:
    values = (d.x1, d.y1, d.x2, d.y2, d.confidence)
    return all(math.isfinite(v) for v in values) and d.x2 > d.x1 and d.y2 > d.y1


def iou_matrix(a: list[Box], b: list[Box]) -> np.ndarray:
    """IoU between every box in ``a`` and every box in ``b`` (shape ``len(a) x len(b)``)."""
    if not a or not b:
        return np.zeros((len(a), len(b)))
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    ix1 = np.maximum(x[:, None, 0], y[None, :, 0])
    iy1 = np.maximum(x[:, None, 1], y[None, :, 1])
    ix2 = np.minimum(x[:, None, 2], y[None, :, 2])
    iy2 = np.minimum(x[:, None, 3], y[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_x = (x[:, 2] - x[:, 0]) * (x[:, 3] - x[:, 1])
    area_y = (y[:, 2] - y[:, 0]) * (y[:, 3] - y[:, 1])
    union = area_x[:, None] + area_y[None, :] - inter
    result: np.ndarray = np.divide(inter, union, out=np.zeros_like(inter), where=union > 0)
    return result


def greedy_match(iou: np.ndarray, threshold: float) -> list[tuple[int, int]]:
    """One-to-one matching, highest IoU first; pairs below ``threshold`` are never matched.

    Deterministic: ties are resolved by (row, column) order thanks to a stable sort.
    """
    if iou.size == 0:
        return []
    rows, cols = np.nonzero(iou >= threshold)
    if rows.size == 0:
        return []
    order = np.argsort(-iou[rows, cols], kind="stable")
    used_r: set[int] = set()
    used_c: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for k in order:
        r, c = int(rows[k]), int(cols[k])
        if r in used_r or c in used_c:
            continue
        used_r.add(r)
        used_c.add(c)
        pairs.append((r, c))
    return pairs


class FaceByteTracker:
    """Stateful tracker for ONE processing session (new instance or `reset` per session)."""

    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.cfg = config or TrackerConfig()
        self._tracks: list[_Track] = []
        self._next_id = 1
        self._last_ts: int | None = None
        self._created = self._confirmed = self._rejected = 0
        self._removed = self._recovered = 0
        self._invalid = self._ts_anomalies = self._refused = 0

    # -- public ---------------------------------------------------------------------
    def reset(self) -> None:
        """Forget everything; ids restart at 1 (a new session)."""
        self.__init__(self.cfg)  # type: ignore[misc]

    def update(
        self,
        detections: list[FaceDetection],
        timestamp_ms: int,
        frame_id: int,
        frame_size: tuple[int, int] | None = None,
    ) -> TrackerOutput:
        cfg = self.cfg
        events: list[TrackEvent] = []

        dt = 0.0
        if self._last_ts is not None:
            if timestamp_ms < self._last_ts:
                self._ts_anomalies += 1  # clock went backwards: treat as "no time passed"
            else:
                dt = (timestamp_ms - self._last_ts) / 1000.0
        self._last_ts = max(timestamp_ms, self._last_ts or 0)

        dets: list[FaceDetection] = []
        for d in detections:
            if _valid(d):
                dets.append(d)
            else:
                self._invalid += 1

        live = [t for t in self._tracks if t.state is not TrackState.REMOVED]
        for t in live:
            t.matched_now = False
            t.cur_conf = None
            decay = cfg.lost_velocity_decay_per_second if t.state is TrackState.LOST else 0.0
            t.kf.predict(dt, decay)

        high_idx = [i for i, d in enumerate(dets) if d.confidence >= cfg.high_confidence]
        low_idx = [i for i, d in enumerate(dets) if d.confidence < cfg.high_confidence]
        free_high = list(high_idx)

        main = sorted(
            (t for t in live if t.state in (TrackState.CONFIRMED, TrackState.LOST)),
            key=lambda t: t.id,
        )
        tent = sorted((t for t in live if t.state is TrackState.TENTATIVE), key=lambda t: t.id)

        # a. confirmed/lost x high (LOST tracks use their own, lower gate)
        matched_main = self._associate(main, dets, free_high, cfg.match_iou, cfg.lost_match_iou)
        free_high = [i for i in free_high if i not in matched_main.values()]
        # b. leftover confirmed/lost x low
        rest = [t for t in main if t.id not in matched_main]
        matched_low = self._associate(rest, dets, low_idx, cfg.low_match_iou)
        # c. tentative x leftover high
        matched_tent = self._associate(tent, dets, free_high, cfg.match_iou)
        free_high = [i for i in free_high if i not in matched_tent.values()]

        by_id = {t.id: t for t in live}
        for mapping in (matched_main, matched_low, matched_tent):
            for track_id, di in mapping.items():
                self._apply_match(by_id[track_id], dets[di], timestamp_ms, frame_id, events)

        # unmatched tracks
        for t in live:
            if not t.matched_now:
                self._apply_miss(t, timestamp_ms, frame_id, events)

        # births
        for di in free_high:
            if sum(1 for t in self._tracks if t.state is not TrackState.REMOVED) >= cfg.max_tracks:
                self._refused += 1
                continue
            track = _Track(self._next_id, dets[di], timestamp_ms, frame_id, cfg)
            self._next_id += 1
            self._created += 1
            self._tracks.append(track)
            if cfg.confirm_hits <= 1:
                self._confirm(track, frame_id, timestamp_ms, events)
            events.append(TrackEvent("created", track.id, frame_id, timestamp_ms))

        self._tracks = [t for t in self._tracks if t.state is not TrackState.REMOVED]
        self._log(events)
        # A TENTATIVE track that was not detected in this frame has no evidence behind it yet, so it
        # is not published (it would only draw a guessed box for something unconfirmed).
        snapshots = [
            self._snapshot(t, timestamp_ms, frame_size)
            for t in sorted(self._tracks, key=lambda t: t.id)
            if not (t.state is TrackState.TENTATIVE and not t.matched_now)
        ]
        return TrackerOutput(
            frame_id=frame_id,
            timestamp_ms=timestamp_ms,
            tracks=[s for s in snapshots if s is not None],
            events=events,
            counts=self.counts(),
        )

    def counts(self) -> TrackerCounts:
        by_state = {s: 0 for s in TrackState}
        for t in self._tracks:
            by_state[t.state] += 1
        return TrackerCounts(
            tentative=by_state[TrackState.TENTATIVE],
            confirmed=by_state[TrackState.CONFIRMED],
            lost=by_state[TrackState.LOST],
            created_total=self._created,
            confirmed_total=self._confirmed,
            rejected_total=self._rejected,
            removed_total=self._removed,
            recovered_total=self._recovered,
            invalid_detections=self._invalid,
            timestamp_anomalies=self._ts_anomalies,
            births_refused=self._refused,
        )

    # -- internals ------------------------------------------------------------------
    def _associate(
        self,
        tracks: list[_Track],
        dets: list[FaceDetection],
        det_indices: list[int],
        threshold: float,
        lost_threshold: float | None = None,
    ) -> dict[int, int]:
        if not tracks or not det_indices:
            return {}
        buffer = self.cfg.box_buffer
        iou = iou_matrix(
            [_grow(t.kf.box(), buffer) for t in tracks],
            [_grow(_box(dets[i]), buffer) for i in det_indices],
        )
        if lost_threshold is None:
            return {tracks[r].id: det_indices[c] for r, c in greedy_match(iou, threshold)}
        # Per-row gate: rows of LOST tracks use `lost_threshold`, the others `threshold`.
        gate = np.array(
            [lost_threshold if t.state is TrackState.LOST else threshold for t in tracks]
        )
        masked = np.where(iou >= gate[:, None], iou, 0.0)
        return {tracks[r].id: det_indices[c] for r, c in greedy_match(masked, 1e-9)}

    def _confirm(self, t: _Track, frame_id: int, ts: int, events: list[TrackEvent]) -> None:
        t.state = TrackState.CONFIRMED
        self._confirmed += 1
        events.append(TrackEvent("confirmed", t.id, frame_id, ts))

    def _apply_match(
        self, t: _Track, det: FaceDetection, ts: int, frame_id: int, events: list[TrackEvent]
    ) -> None:
        t.kf.update(_box(det))
        t.matched_now = True
        t.hits += 1
        t.missed = 0
        t.last_seen_ms = ts
        t.last_conf = det.confidence
        t.cur_conf = det.confidence
        t.last_frame = frame_id
        if t.state is TrackState.LOST:
            t.state = TrackState.CONFIRMED
            self._recovered += 1
            events.append(TrackEvent("recovered", t.id, frame_id, ts))
        elif t.state is TrackState.TENTATIVE and t.hits >= self.cfg.confirm_hits:
            self._confirm(t, frame_id, ts, events)

    def _apply_miss(self, t: _Track, ts: int, frame_id: int, events: list[TrackEvent]) -> None:
        t.missed += 1
        gap = (ts - t.last_seen_ms) / 1000.0
        if t.state is TrackState.CONFIRMED:
            t.state = TrackState.LOST
            events.append(TrackEvent("lost", t.id, frame_id, ts))
        if t.state is TrackState.TENTATIVE and gap > self.cfg.tentative_timeout_seconds:
            t.state = TrackState.REMOVED
            self._rejected += 1
            events.append(TrackEvent("rejected", t.id, frame_id, ts))
        elif t.state is TrackState.LOST and gap > self.cfg.max_missing_seconds:
            t.state = TrackState.REMOVED
            self._removed += 1
            events.append(TrackEvent("removed", t.id, frame_id, ts, "expired"))

    def _snapshot(
        self, t: _Track, ts: int, frame_size: tuple[int, int] | None
    ) -> TrackSnapshot | None:
        x1, y1, x2, y2 = t.kf.box()
        if frame_size is not None:
            w, h = frame_size
            x1, y1, x2, y2 = max(0.0, x1), max(0.0, y1), min(float(w), x2), min(float(h), y2)
        if not (x2 - x1 >= 1.0 and y2 - y1 >= 1.0):
            return None  # entirely outside the picture: still tracked, nothing to draw
        return TrackSnapshot(
            track_id=t.id,
            state=t.state,
            bbox=(x1, y1, x2, y2),
            observed=t.matched_now,
            detection_confidence=t.cur_conf if t.matched_now else None,
            last_observed_confidence=t.last_conf,
            first_seen_ms=t.first_ms,
            last_seen_ms=t.last_seen_ms,
            age_ms=max(0, ts - t.first_ms),
            hits=t.hits,
            missed_updates=t.missed,
            last_update_frame=t.last_frame,
        )

    @staticmethod
    def _log(events: list[TrackEvent]) -> None:
        for e in events:
            level = logging.DEBUG if e.kind in ("created", "rejected") else logging.INFO
            logger.log(
                level,
                "track.%s",
                e.kind,
                extra={"track_id": e.track_id, "frame_id": e.frame_id, "ts_ms": e.timestamp_ms},
            )
