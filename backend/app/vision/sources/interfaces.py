"""Extension point: video source adapters (file, RTSP). Implemented in Phase 1."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Frame:
    index: int
    timestamp_seconds: float
    pixels: Any  # numpy ndarray (BGR) once OpenCV is introduced; untyped to keep Phase 0 light


class VideoSource(Protocol):
    def open(self) -> None: ...
    def frames(self) -> Iterator[Frame]: ...
    def close(self) -> None: ...
