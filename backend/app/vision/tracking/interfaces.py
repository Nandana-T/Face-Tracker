"""Extension point: multi-face tracking (ByteTrack or equivalent). Phase 3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.vision.detection.interfaces import FaceDetection


@dataclass(frozen=True)
class TrackedFace:
    track_id: int  # temporary, per-session; NOT a visitor identity
    detection: FaceDetection


class FaceTracker(Protocol):
    def update(
        self, detections: list[FaceDetection], timestamp_seconds: float
    ) -> list[TrackedFace]: ...
