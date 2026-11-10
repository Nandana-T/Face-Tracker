"""Extension point: face detectors (YOLO-based). Implemented in Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.vision.sources.interfaces import Frame


@dataclass(frozen=True)
class FaceDetection:
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float


class FaceDetector(Protocol):
    def detect(self, frame: Frame) -> list[FaceDetection]: ...
