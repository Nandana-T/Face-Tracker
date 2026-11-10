"""Multi-face tracking contract (Phase 2).

A *track* is a temporary, session-scoped hypothesis "these detections over time are the same face".
Track ids restart for every processing session and are NEVER a visitor identity: nothing here knows
who anyone is, and a person who leaves and returns (or is hidden for longer than the lost-time
limit) gets a new track id.

Lifecycle (deterministic, time-based)::

    (unmatched detection) -> TENTATIVE --confirm_hits observed updates--> CONFIRMED
    TENTATIVE --no detection for tentative_timeout_seconds--> REMOVED  (counted as "rejected")
    CONFIRMED --update without a matching detection--> LOST
    LOST --matching detection--> CONFIRMED   (a "recovery"; the id is kept)
    LOST --no detection for max_missing_seconds--> REMOVED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.vision.detection.interfaces import FaceDetection


class TrackState(StrEnum):
    TENTATIVE = "TENTATIVE"
    CONFIRMED = "CONFIRMED"
    LOST = "LOST"
    REMOVED = "REMOVED"


@dataclass(frozen=True)
class TrackSnapshot:
    """Immutable view of one track after an update. Times are source-clock milliseconds."""

    track_id: int
    state: TrackState
    bbox: tuple[float, float, float, float]  # x1, y1, x2, y2 (observed or predicted)
    observed: bool  # True only if a detection was matched in THIS update
    # Score of the detection matched in this update; None for predicted boxes (never invented).
    detection_confidence: float | None
    last_observed_confidence: float | None
    first_seen_ms: int
    last_seen_ms: int  # last time a detection was matched
    age_ms: int  # current update time - first_seen_ms
    hits: int  # number of observed updates
    missed_updates: int  # consecutive updates without a detection
    last_update_frame: int  # frame index of the last observed update


@dataclass(frozen=True)
class TrackEvent:
    kind: str  # created | confirmed | lost | recovered | removed | rejected
    track_id: int
    frame_id: int
    timestamp_ms: int
    detail: str = ""


@dataclass(frozen=True)
class TrackerCounts:
    tentative: int = 0
    confirmed: int = 0
    lost: int = 0
    created_total: int = 0
    confirmed_total: int = 0  # tracks that ever reached CONFIRMED
    rejected_total: int = 0  # tentative tracks dropped before confirmation
    removed_total: int = 0  # confirmed tracks that expired
    recovered_total: int = 0  # LOST -> CONFIRMED transitions
    invalid_detections: int = 0
    timestamp_anomalies: int = 0
    births_refused: int = 0  # new tracks not created because max_tracks was reached


@dataclass(frozen=True)
class TrackerOutput:
    frame_id: int
    timestamp_ms: int
    tracks: list[TrackSnapshot]  # TENTATIVE, CONFIRMED and LOST tracks, ordered by track_id
    events: list[TrackEvent] = field(default_factory=list)
    counts: TrackerCounts = field(default_factory=TrackerCounts)


class FaceTracker(Protocol):
    def update(
        self,
        detections: list[FaceDetection],
        timestamp_ms: int,
        frame_id: int,
        frame_size: tuple[int, int] | None = None,
    ) -> TrackerOutput: ...

    def reset(self) -> None: ...
