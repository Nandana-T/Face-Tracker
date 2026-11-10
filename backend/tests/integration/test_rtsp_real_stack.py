"""The real OpenCV/FFmpeg RTSP code path, against an address nothing listens on.

This is NOT a camera test: no RTSP stream was ever served. It only proves that the production
capture factory (timeouts passed to OpenCV) fails fast and that the adapter converts the failure to
the documented error codes instead of hanging. A genuine RTSP camera has not been tested.
"""

from __future__ import annotations

import time

import pytest

from app.vision.sources.interfaces import SourceOpenError, SourceUnavailable
from app.vision.sources.rtsp_source import RtspVideoSource

pytestmark = pytest.mark.e2e

CLOSED = "rtsp://user:secret@127.0.0.1:1/none"  # port 1: connection refused / timeout


def test_unreachable_address_fails_within_the_configured_timeout() -> None:
    src = RtspVideoSource(CLOSED, connect_timeout_s=2, read_timeout_s=2)
    started = time.monotonic()
    with pytest.raises(SourceOpenError) as exc:
        src.open()
    assert exc.value.code == "camera_unavailable"
    assert time.monotonic() - started < 8
    assert "secret" not in str(exc.value)
    src.close()


def test_reconnect_loop_is_bounded_with_the_real_capture() -> None:
    class DeadCapture:
        def read(self) -> tuple[bool, None]:
            return False, None

        def get(self, _prop: int) -> float:
            return 0

        def release(self) -> None:
            pass

    src = RtspVideoSource(
        CLOSED,
        connect_timeout_s=1,
        read_timeout_s=1,
        reconnect_interval_s=0.1,
        max_reconnect_attempts=2,
    )
    src._cap = DeadCapture()  # a connection that dies right after it was established
    started = time.monotonic()
    with pytest.raises(SourceUnavailable):
        list(src.frames())
    assert time.monotonic() - started < 10
