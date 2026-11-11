from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from app.vision.sources.file_source import FileVideoSource
from app.vision.sources.interfaces import SourceCorrupted, SourceOpenError, SourceUnavailable
from app.vision.sources.registry import build_source, get_descriptor, list_sources
from app.vision.sources.rtsp_source import RtspVideoSource, redact_url
from tests.conftest import make_full_config


def write_video(
    path: Path, frames: int = 12, fps: int = 10, size: tuple[int, int] = (64, 48)
) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), fps, size)
    for i in range(frames):
        writer.write(np.full((size[1], size[0], 3), i * 10 % 255, dtype=np.uint8))
    writer.release()
    return path


# ---- file source -----------------------------------------------------------------------------
def test_reads_every_frame_then_signals_eof(tmp_path: Path) -> None:
    src = FileVideoSource(write_video(tmp_path / "v.mp4", frames=12, fps=10))
    info = src.open()
    assert (info.width, info.height) == (64, 48)
    assert info.fps == pytest.approx(10)
    assert not info.is_live
    frames = list(src.frames())
    src.close()
    assert [f.index for f in frames] == list(range(12))  # sequential, 0-based
    assert frames[0].timestamp_ms == 0
    assert frames[5].timestamp_ms == 500  # index / fps
    assert frames[0].pixels.shape == (48, 64, 3)
    assert src.corrupt_frames == 0


def test_missing_and_invalid_files(tmp_path: Path) -> None:
    with pytest.raises(SourceOpenError) as e1:
        FileVideoSource(tmp_path / "missing.mp4").open()
    assert e1.value.code == "source_not_found"
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"definitely not a video" * 100)
    with pytest.raises(SourceOpenError) as e2:
        FileVideoSource(junk).open()
    assert e2.value.code == "source_invalid"


def test_close_is_idempotent_and_releases(tmp_path: Path) -> None:
    src = FileVideoSource(write_video(tmp_path / "v.mp4"))
    src.open()
    it = src.frames()
    next(it)
    src.close()
    src.close()  # second close must not raise
    assert list(it) == []  # iteration stops cleanly after close


def test_frames_before_open_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceOpenError):
        next(iter(FileVideoSource(tmp_path / "x.mp4").frames()))


class ScriptedCapture:
    """cv2.VideoCapture stand-in that replays ok/fail read results."""

    def __init__(self, script: list[bool], count: int | None = None, opened: bool = True) -> None:
        self.script = list(script)
        self.count = len(script) if count is None else count
        self.opened = opened
        self.released = False

    def isOpened(self) -> bool:  # noqa: N802
        return self.opened

    def get(self, prop: int) -> float:
        return {
            cv2.CAP_PROP_FRAME_WIDTH: 32,
            cv2.CAP_PROP_FRAME_HEIGHT: 24,
            cv2.CAP_PROP_FPS: 10,
            cv2.CAP_PROP_FRAME_COUNT: self.count,
        }.get(prop, 0)

    def read(self) -> tuple[bool, Any]:
        if not self.script:
            return False, None
        ok = self.script.pop(0)
        return ok, (np.zeros((24, 32, 3), dtype=np.uint8) if ok else None)

    def release(self) -> None:
        self.released = True


def scripted_source(
    tmp_path: Path, script: list[bool], count: int | None = None, max_failures: int = 3
) -> tuple[FileVideoSource, ScriptedCapture]:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    cap = ScriptedCapture(script, count=count)
    src = FileVideoSource(f, max_consecutive_failures=max_failures, capture_factory=lambda _p: cap)
    return src, cap


def test_corrupt_frames_are_skipped_and_counted(tmp_path: Path) -> None:
    src, cap = scripted_source(tmp_path, [True, False, True, True, False, True])
    src.open()
    frames = list(src.frames())
    assert len(frames) == 4
    assert src.corrupt_frames == 2
    assert [f.index for f in frames] == [0, 1, 2, 3]  # undecodable frames consume no index
    src.close()
    assert cap.released


def test_too_many_consecutive_failures_raise(tmp_path: Path) -> None:
    src, _ = scripted_source(tmp_path, [True] + [False] * 8, count=9)
    src.open()
    with pytest.raises(SourceCorrupted):
        list(src.frames())


def test_unopenable_capture_is_reported(tmp_path: Path) -> None:
    f = tmp_path / "x.mp4"
    f.write_bytes(b"x")
    cap = ScriptedCapture([], opened=False)
    with pytest.raises(SourceOpenError):
        FileVideoSource(f, capture_factory=lambda _p: cap).open()
    assert cap.released


# ---- RTSP source (controlled fakes - no camera is contacted) ---------------------------------
class FakeRtsp:
    def __init__(self, plan: list[list[bool]], open_results: list[bool]) -> None:
        self.plan = plan
        self.open_results = list(open_results)
        self.caps: list[ScriptedCapture] = []

    def factory(self, url: str, connect_timeout: float, read_timeout: float) -> ScriptedCapture:
        opened = self.open_results.pop(0) if self.open_results else True
        # Only a successful connection consumes a read script.
        script = self.plan.pop(0) if (opened and self.plan) else []
        cap = ScriptedCapture(script, opened=opened)
        self.caps.append(cap)
        return cap


def make_rtsp(fake: FakeRtsp, **kw: Any) -> tuple[RtspVideoSource, list[float]]:
    sleeps: list[float] = []
    src = RtspVideoSource(
        "rtsp://user:secret@10.0.0.9:554/stream",
        capture_factory=fake.factory,
        sleep=sleeps.append,
        reconnect_interval_s=1,
        max_backoff_s=4,
        **kw,
    )
    return src, sleeps


def test_rtsp_unreachable_camera_on_open() -> None:
    src, _ = make_rtsp(FakeRtsp([[]], [False]))
    with pytest.raises(SourceOpenError) as exc:
        src.open()
    assert exc.value.code == "camera_unavailable"


def test_rtsp_reconnects_after_read_failure_with_backoff() -> None:
    states: list[str] = []
    # open ok; two frames; read fails -> reconnect attempts: fail, fail, succeed -> one more frame
    fake = FakeRtsp([[True, True], [True]], [True, False, False, True])
    src, sleeps = make_rtsp(fake, on_state=states.append)
    src.open()
    got = []
    for frame in src.frames():
        got.append(frame.index)
        if len(got) == 3:
            break
    assert got == [0, 1, 2]  # indices continue across the reconnect
    assert sleeps == [1, 2, 4]  # exponential backoff, capped at max_backoff
    assert src.reconnects == 1
    assert states == ["reconnecting", "connected"]
    assert fake.caps[0].released  # the dead connection was released
    src.close()
    assert fake.caps[-1].released


class StallingCapture(ScriptedCapture):
    """Delivers its frames, then blocks in read() like a connected-but-silent RTSP server."""

    def __init__(self, frames: int) -> None:
        super().__init__([True] * frames)
        self.unblock = threading.Event()

    def read(self) -> tuple[bool, Any]:
        if self.script:
            return super().read()
        self.unblock.wait(5)
        return False, None


def test_rtsp_stalled_stream_is_detected_and_reconnected() -> None:
    caps: list[ScriptedCapture] = []

    def factory(url: str, connect_timeout: float, read_timeout: float) -> ScriptedCapture:
        cap: ScriptedCapture = StallingCapture(2) if not caps else ScriptedCapture([True, True])
        caps.append(cap)
        return cap

    states: list[str] = []
    src = RtspVideoSource(
        "rtsp://10.0.0.9/stream", capture_factory=factory, sleep=lambda _s: None, reconnect_interval_s=1,
        read_timeout_s=0.05, stall_grace_s=0.1, on_state=states.append,
    )  # fmt: skip
    src.open()
    got = []
    for frame in src.frames():
        got.append(frame.index)
        if len(got) == 4:
            break
    assert got == [0, 1, 2, 3]  # the stall did not hang the source; indices continue
    assert src.stalls == 1 and src.reconnects == 1 and states == ["reconnecting", "connected"]
    stuck = caps[0]
    assert isinstance(stuck, StallingCapture)
    stuck.unblock.set()  # the blocked read finally returns -> the abandoned capture is released
    deadline = time.time() + 2
    while not stuck.released and time.time() < deadline:
        time.sleep(0.01)
    assert stuck.released
    src.close()


def test_rtsp_gives_up_after_max_attempts() -> None:
    fake = FakeRtsp([[]], [True, False, False])
    src, _ = make_rtsp(fake, max_reconnect_attempts=2)
    src.open()
    with pytest.raises(SourceUnavailable):
        list(src.frames())


def test_rtsp_stop_during_reconnect_is_graceful() -> None:
    fake = FakeRtsp([[]], [True, False])
    holder: dict[str, RtspVideoSource] = {}

    def sleeper(_s: float) -> None:
        holder["src"].close()  # shutdown requested while waiting to reconnect

    src = RtspVideoSource("rtsp://h/x", capture_factory=fake.factory, sleep=sleeper)
    holder["src"] = src
    src.open()
    assert list(src.frames()) == []  # ends without raising
    assert all(c.released for c in fake.caps)


def test_rtsp_credentials_never_appear_in_logged_url() -> None:
    redacted = redact_url("rtsp://admin:hunter2@192.168.1.5:554/live?token=abc")
    assert redacted == "rtsp://192.168.1.5:554/live"
    src, _ = make_rtsp(FakeRtsp([], []))
    assert "secret" not in src.url_for_log
    assert "user" not in src.url_for_log


# ---- registry (trusted server-side sources) --------------------------------------------------
def test_registry_never_leaks_locations(tmp_path: Path) -> None:
    video = write_video(tmp_path / "clip.mp4")
    (tmp_path / "samples").mkdir()
    write_video(tmp_path / "samples" / "My Sample.mp4")
    cfg = make_full_config(
        tmp_path,
        video={
            "sample_dir": str(tmp_path / "samples"),
            "sources": [
                {"id": "clip", "label": "Clip", "type": "file", "path": str(video)},
                {"id": "cam", "label": "Camera", "type": "rtsp", "url_env": "TEST_RTSP_URL"},
                {"id": "gone", "label": "Gone", "type": "file", "path": str(tmp_path / "gone.mp4")},
            ],
        },
    )
    env = {"TEST_RTSP_URL": "rtsp://admin:pw@10.1.1.1/s"}
    listed = {d.id: d for d in list_sources(cfg, env)}
    assert listed["clip"].availability == "available"
    assert listed["gone"].availability == "unavailable"
    assert listed["cam"].availability == "available"
    assert "sample-my-sample" in listed
    blob = repr(list(listed.values()))
    assert "pw" not in blob
    assert str(tmp_path) not in blob
    assert "10.1.1.1" not in blob
    unset = {d.id: d for d in list_sources(cfg, {})}
    assert unset["cam"].availability == "unavailable"  # env var not set
    assert get_descriptor(cfg, "nope", env) is None


def test_build_source_validates(tmp_path: Path) -> None:
    cfg = make_full_config(tmp_path)
    with pytest.raises(SourceOpenError) as exc:
        build_source(cfg, "unknown", {})
    assert exc.value.code == "source_not_found"
