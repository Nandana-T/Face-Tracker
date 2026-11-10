"""Face-detector contract. Implementations are interchangeable (benchmarking, future models).

Coordinate convention: ``(x1, y1, x2, y2)`` in *pixels of the original frame* (not the resized /
letterboxed model input), origin at the top-left, x to the right, y downwards, ``x1 < x2`` and
``y1 < y2``, clipped to ``[0, width] x [0, height]``. ``confidence`` is in ``[0, 1]``.
"""

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

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1


class DetectorError(Exception):
    code = "detector_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ModelUnavailable(DetectorError):
    """The model file is missing/corrupt/incompatible. The message is actionable."""

    code = "model_unavailable"


class InvalidFrame(DetectorError):
    code = "invalid_frame"


class FaceDetector(Protocol):
    name: str

    def detect(self, frame: Frame) -> list[FaceDetection]: ...

    def close(self) -> None: ...
