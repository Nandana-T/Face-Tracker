"""Video source abstraction shared by the file and RTSP adapters.

Conventions (see docs/phase-1-report.md):
* ``Frame.index`` is the 0-based position of the frame among frames *delivered by the source*
  (frames that could not be decoded are counted separately and do not consume an index).
* ``Frame.timestamp_ms`` is milliseconds since the start of the source: ``index / fps`` for files,
  monotonic wall-clock since open for live streams.
* ``Frame.pixels`` is a BGR uint8 numpy array in the *original* source resolution.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Frame:
    index: int
    timestamp_ms: int
    pixels: Any  # numpy.ndarray (H, W, 3) BGR uint8

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])


@dataclass(frozen=True)
class SourceInfo:
    width: int
    height: int
    fps: float
    frame_count: int | None  # None for live streams / unknown
    is_live: bool


class SourceError(Exception):
    """Base class. ``code`` is a stable machine-readable identifier safe to show to users."""

    code = "source_error"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class SourceOpenError(SourceError):
    code = "source_open_failed"


class SourceUnavailable(SourceError):
    """A live source cannot be (re)connected within the configured limits."""

    code = "camera_unavailable"


class SourceCorrupted(SourceError):
    code = "source_corrupted"


class VideoSource(Protocol):
    """Lifecycle: ``open()`` -> iterate ``frames()`` -> ``close()`` (idempotent, always safe)."""

    @property
    def is_live(self) -> bool: ...

    def open(self) -> SourceInfo: ...

    def frames(self) -> Iterator[Frame]:
        """Yield frames until end-of-stream (files) or ``close()``/unrecoverable failure.

        Raises ``SourceCorrupted`` / ``SourceUnavailable`` when it cannot continue.
        """
        ...

    def close(self) -> None: ...
