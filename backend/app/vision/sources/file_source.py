"""Local video-file source (OpenCV/FFmpeg)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import cv2

from app.vision.sources.interfaces import (
    Frame,
    SourceCorrupted,
    SourceInfo,
    SourceOpenError,
)

logger = logging.getLogger("katomaran.video.file")

CaptureFactory = Callable[[str], Any]


class FileVideoSource:
    """Sequential reader. End-of-file is distinguished from corruption using the container's
    frame count: a failed read at/after the last frame is EOF, earlier failures are corrupt frames
    that are skipped up to ``max_consecutive_failures`` in a row."""

    is_live = False

    def __init__(
        self,
        path: Path,
        max_consecutive_failures: int = 30,
        capture_factory: CaptureFactory = cv2.VideoCapture,
    ) -> None:
        self._path = path
        self._max_failures = max_consecutive_failures
        self._factory = capture_factory
        self._cap: Any = None
        self._info: SourceInfo | None = None
        self._lock = threading.Lock()
        self.corrupt_frames = 0

    def open(self) -> SourceInfo:
        if not self._path.is_file():
            raise SourceOpenError("The video file does not exist.", "source_not_found")
        cap = self._factory(str(self._path))
        if not cap.isOpened():
            cap.release()
            raise SourceOpenError("The file could not be opened as a video.", "source_invalid")
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if width <= 0 or height <= 0:
            cap.release()
            raise SourceOpenError("The video has invalid frame dimensions.", "source_invalid")
        if not (0 < fps < 1000):
            logger.warning("Video reports unusable FPS (%s); assuming 25", fps)
            fps = 25.0
        with self._lock:
            self._cap = cap
        self._info = SourceInfo(
            width=width,
            height=height,
            fps=fps,
            frame_count=count if count > 0 else None,
            is_live=False,
        )
        logger.info(
            "Video file opened",
            extra={"width": width, "height": height, "fps": round(fps, 2), "frame_count": count},
        )
        return self._info

    def frames(self) -> Iterator[Frame]:
        if self._cap is None or self._info is None:
            raise SourceOpenError("The source is not open.", "source_not_open")
        info = self._info
        index = 0
        consecutive = 0
        position = 0  # frames the decoder has been asked for (delivered + corrupt)
        while True:
            with self._lock:
                cap = self._cap
                if cap is None:  # closed concurrently
                    return
                ok, pixels = cap.read()
            if ok and pixels is not None and getattr(pixels, "size", 0) > 0:
                consecutive = 0
                position += 1
                yield Frame(index=index, timestamp_ms=int(index * 1000 / info.fps), pixels=pixels)
                index += 1
                continue
            position += 1
            # A failed read is EOF unless the container promised more frames than were requested.
            if info.frame_count is None or position > info.frame_count:
                return
            self.corrupt_frames += 1
            consecutive += 1
            logger.warning("Undecodable frame skipped", extra={"position": position})
            if consecutive >= self._max_failures:
                raise SourceCorrupted(
                    f"{consecutive} consecutive frames could not be decoded.", "source_corrupted"
                )

    def close(self) -> None:
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
            logger.info("Video file closed")
