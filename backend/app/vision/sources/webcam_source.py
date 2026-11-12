"""The computer's own camera (USB / built-in) as a live source.

Reuses the RTSP source's live behaviour (no EOF, stall watchdog, bounded reconnection) with a
device index instead of a URL. A camera that disappears (unplugged, taken by another app) ends the
session after a few reconnect attempts instead of retrying forever.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any

import cv2

from app.vision.sources.rtsp_source import RtspVideoSource


def _device_capture(index_text: str, _connect_timeout_s: float, _read_timeout_s: float) -> Any:
    # DirectShow opens quickly on Windows; other platforms use OpenCV's default backend.
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    return cv2.VideoCapture(int(index_text), backend)


class WebcamVideoSource(RtspVideoSource):
    def __init__(
        self,
        index: int = 0,
        reconnect_interval_s: float = 2,
        max_reconnect_attempts: int = 5,
        on_state: Callable[[str], None] | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("capture_factory", _device_capture)
        super().__init__(
            str(index),
            reconnect_interval_s=reconnect_interval_s,
            max_backoff_s=10,
            max_reconnect_attempts=max_reconnect_attempts,
            on_state=on_state,
            **kwargs,
        )
        self.url_for_log = f"webcam:{index}"
