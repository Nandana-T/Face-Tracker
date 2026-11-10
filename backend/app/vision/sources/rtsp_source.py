"""RTSP (live network camera) source with timeouts and a reconnection strategy.

Transport behaviour differs from files (no EOF, frames are dropped rather than waited for, the
connection can vanish); that is encapsulated here so callers only see ``VideoSource``.

Credentials: the URL is held privately and only ever logged in redacted form.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import cv2

from app.vision.sources.interfaces import (
    Frame,
    SourceInfo,
    SourceOpenError,
    SourceUnavailable,
)

logger = logging.getLogger("katomaran.video.rtsp")

CaptureFactory = Callable[[str, float, float], Any]


def redact_url(url: str) -> str:
    """Remove userinfo (credentials) from a URL for safe logging."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "<unparseable url>"


def _opencv_capture(url: str, connect_timeout_s: float, read_timeout_s: float) -> Any:
    # Must be set before the capture is created; applies to FFmpeg-backed captures.
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    return cv2.VideoCapture(
        url,
        cv2.CAP_FFMPEG,
        [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            int(connect_timeout_s * 1000),
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            int(read_timeout_s * 1000),
        ],
    )


class RtspVideoSource:
    is_live = True

    def __init__(
        self,
        url: str,
        connect_timeout_s: float = 10,
        read_timeout_s: float = 10,
        reconnect_interval_s: float = 5,
        max_backoff_s: float = 30,
        max_reconnect_attempts: int = 0,
        capture_factory: CaptureFactory = _opencv_capture,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_state: Callable[[str], None] | None = None,
    ) -> None:
        self._url = url
        self._connect_timeout = connect_timeout_s
        self._read_timeout = read_timeout_s
        self._interval = reconnect_interval_s
        self._max_backoff = max_backoff_s
        self._max_attempts = max_reconnect_attempts
        self._factory = capture_factory
        self._stop = threading.Event()
        self._sleep = sleep or (lambda s: self._stop.wait(s))
        self._clock = clock
        self._on_state = on_state or (lambda _s: None)
        self._cap: Any = None
        self._lock = threading.Lock()
        self._info: SourceInfo | None = None
        self.reconnects = 0
        self.url_for_log = redact_url(url)

    # -- connection -----------------------------------------------------------------
    def _connect(self) -> Any:
        cap = self._factory(self._url, self._connect_timeout, self._read_timeout)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def open(self) -> SourceInfo:
        cap = self._connect()
        if cap is None:
            raise SourceOpenError("The camera could not be reached.", "camera_unavailable")
        with self._lock:
            self._cap = cap
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        self._info = SourceInfo(
            width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            fps=fps if 0 < fps < 1000 else 25.0,
            frame_count=None,
            is_live=True,
        )
        logger.info("RTSP stream opened", extra={"url": self.url_for_log})
        return self._info

    def _reconnect(self) -> bool:
        """Retry with exponential backoff. Returns False when stopped; raises when exhausted."""
        self._on_state("reconnecting")
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            if self._max_attempts and attempt > self._max_attempts:
                raise SourceUnavailable(
                    f"The camera stayed unavailable after {self._max_attempts} reconnect attempts.",
                    "camera_unavailable",
                )
            delay = min(self._interval * (2 ** (attempt - 1)), self._max_backoff)
            logger.warning(
                "RTSP reconnect attempt",
                extra={"attempt": attempt, "delay_s": delay, "url": self.url_for_log},
            )
            self._sleep(delay)
            if self._stop.is_set():
                return False
            cap = self._connect()
            if cap is not None:
                with self._lock:
                    old, self._cap = self._cap, cap
                if old is not None:
                    old.release()
                self.reconnects += 1
                logger.info("RTSP stream reconnected", extra={"url": self.url_for_log})
                self._on_state("connected")
                return True
        return False

    # -- frames ---------------------------------------------------------------------
    def frames(self) -> Iterator[Frame]:
        if self._cap is None:
            raise SourceOpenError("The source is not open.", "source_not_open")
        index = 0
        started = self._clock()
        while not self._stop.is_set():
            with self._lock:
                cap = self._cap
            if cap is None:
                return
            ok, pixels = cap.read()
            if ok and pixels is not None and getattr(pixels, "size", 0) > 0:
                yield Frame(
                    index=index,
                    timestamp_ms=int((self._clock() - started) * 1000),
                    pixels=pixels,
                )
                index += 1
                continue
            if self._stop.is_set():
                return
            logger.warning("RTSP read failed", extra={"url": self.url_for_log})
            if not self._reconnect():
                return

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            cap, self._cap = self._cap, None
        if cap is not None:
            cap.release()
            logger.info("RTSP stream closed", extra={"url": self.url_for_log})
