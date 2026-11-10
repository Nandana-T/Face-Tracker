"""End-to-end backend slice: real uvicorn server + WorkerAgent + real YOLO model + real video.

Everything crosses real sockets (HTTP, worker WebSocket, browser WebSocket); only the database is
SQLite (session logic) - the PostgreSQL schema is covered by test_database.py. The sample video is
synthetic (a public-domain portrait panned/zoomed); it proves the plumbing and coordinate
alignment with a real face, not accuracy on real footage.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker
from websockets.sync.client import connect

from app.core.settings import Settings, get_settings
from app.main import create_app
from app.services.session_store import SessionStore
from app.vision.sample_video import build_sample_video, portrait_rects
from app.workers import protocol as P
from app.workers.agent import WorkerAgent
from tests.conftest import make_full_config, make_settings

pytestmark = [pytest.mark.e2e, pytest.mark.model]

SKIP = 2
FPS = 25
SECONDS = 4
TOTAL = FPS * SECONDS
TAIL_SECONDS = 1


class Stack:
    def __init__(self, tmp_path: Path, engine: Engine, models_dir: Path | None = None) -> None:
        self.tmp = tmp_path
        self.video = tmp_path / "two-faces.mp4"
        build_sample_video(
            self.video, seconds=SECONDS, fps=FPS, faces=2, face_free_tail_seconds=TAIL_SECONDS
        )
        self.junk = tmp_path / "broken.mp4"
        self.junk.write_bytes(b"not a video" * 200)
        self.long_video = tmp_path / "long.mp4"
        build_sample_video(self.long_video, seconds=30, fps=FPS, faces=1)
        self.cfg = make_full_config(
            tmp_path,
            video={
                "sample_dir": str(tmp_path / "none"),
                "sources": [
                    {"id": "two", "label": "Two faces", "type": "file", "path": str(self.video)},
                    {"id": "long", "label": "Long", "type": "file", "path": str(self.long_video)},
                    {"id": "broken", "label": "Broken", "type": "file", "path": str(self.junk)},
                ],
            },
            detection={"frame_skip": SKIP},
            worker={
                "realtime_pacing": False,
                "heartbeat_interval_seconds": 1,
                "stats_interval_seconds": 0.2,
            },
        )
        real = get_settings()
        self.api_settings = make_settings(tmp_path, worker_token=SecretStr("e2e-token"))
        self.worker_settings: Settings = make_settings(
            tmp_path,
            worker_token=SecretStr("e2e-token"),
            models_dir=models_dir or real.models_dir,
            worker_id="e2e-worker",
        )
        self.engine = engine
        self.store = SessionStore(sessionmaker(bind=engine, expire_on_commit=False))
        self.server: uvicorn.Server | None = None
        self.agent: WorkerAgent | None = None
        self.threads: list[threading.Thread] = []
        self.port = 0
        self.client: httpx.Client | None = None

    def start(self) -> None:
        app = create_app(settings=self.api_settings, config=self.cfg, engine=self.engine)
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=0, log_level="warning", ws="websockets-sansio"
            )
        )
        t = threading.Thread(target=self.server.run, daemon=True)
        t.start()
        self.threads.append(t)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (self.server.started and self.server.servers):
            time.sleep(0.05)
        assert self.server.started, "uvicorn did not start"
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}/api/v1", timeout=10)
        self.start_worker()

    def start_worker(self, detector_factory: Callable[..., Any] | None = None) -> None:
        settings = self.worker_settings.model_copy(
            update={"worker_api_url": f"ws://127.0.0.1:{self.port}"}
        )
        kwargs: dict[str, Any] = {}
        if detector_factory is not None:
            kwargs["detector_factory"] = detector_factory
        self.agent = WorkerAgent(settings, self.cfg, **kwargs)
        agent = self.agent
        t = threading.Thread(target=lambda: asyncio.run(agent.run_forever()), daemon=True)
        t.start()
        self.threads.append(t)
        self.wait(lambda: self.get("/sources")["worker"]["connected"], "worker connect")

    def get(self, path: str) -> Any:
        assert self.client is not None
        return self.client.get(path).json()

    def wait(self, predicate: Callable[[], Any], what: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}")

    def start_session(self, source: str) -> dict[str, Any]:
        assert self.client is not None
        r = self.client.post("/sessions", json={"source_id": source})
        assert r.status_code in (200, 202), r.text
        return r.json()  # type: ignore[no-any-return]

    def session(self, sid: str) -> dict[str, Any]:
        return self.get(f"/sessions/{sid}")  # type: ignore[no-any-return]

    def wait_state(self, sid: str, states: tuple[str, ...], timeout: float = 30) -> dict[str, Any]:
        self.wait(lambda: self.session(sid)["state"] in states, f"{sid} in {states}", timeout)
        return self.session(sid)

    def stop(self) -> None:
        if self.agent:
            self.agent.request_shutdown()
        if self.server:
            self.server.should_exit = True
        for t in self.threads:
            t.join(timeout=15)
        if self.client:
            self.client.close()


@pytest.fixture
def stack(tmp_path: Path, sqlite_engine: Engine) -> Iterator[Stack]:
    s = Stack(tmp_path, sqlite_engine)
    s.start()
    yield s
    s.stop()


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


# Face box inside the 512x512 portrait, as found by the real detector on the original photograph
# (180,57)-(271,178). Used to project the expected position into each video frame.
FACE_IN_PORTRAIT = (180 / 512, 57 / 512, 271 / 512, 178 / 512)


def expected_boxes(frame_id: int) -> list[tuple[float, float, float, float]]:
    out = []
    for k, (x0, y0, side) in enumerate(portrait_rects(frame_id, TOTAL, faces=2)):
        fx1, fy1, fx2, fy2 = FACE_IN_PORTRAIT
        if k == 1:  # mirrored copy
            fx1, fx2 = 1 - fx2, 1 - fx1
        out.append((x0 + fx1 * side, y0 + fy1 * side, x0 + fx2 * side, y0 + fy2 * side))
    return out


def test_real_video_is_processed_end_to_end(stack: Stack) -> None:
    started = stack.start_session("two")
    sid = started["id"]
    assert started["state"] == "STARTING"
    # A browser subscribes to the live stream like the dashboard does.
    frames: list[tuple[dict[str, Any], int]] = []
    states: list[str] = []
    with connect(
        f"ws://127.0.0.1:{stack.port}/api/v1/sessions/{sid}/stream", max_size=P.MAX_FRAME_BYTES
    ) as browser:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                message = browser.recv(timeout=5)
            except TimeoutError:
                continue
            except Exception:
                break
            if isinstance(message, bytes):
                header, jpeg = P.unpack_frame(message)
                frames.append((header, len(jpeg)))
            else:
                data = json.loads(message)
                states.append(data["type"])
                if data["type"] == "ended":
                    break
    final = stack.wait_state(sid, ("COMPLETED", "ERROR"))
    assert final["state"] == "COMPLETED", final
    assert final["stop_reason"] == "end_of_stream"

    # --- ingestion + frame skipping ---------------------------------------------------------
    stats = final["stats"]
    assert stats["frames_read"] == TOTAL
    expected_processed = len(range(0, TOTAL, SKIP + 1))
    assert stats["frames_processed"] == expected_processed
    assert stats["frames_skipped"] == TOTAL - expected_processed
    assert stats["frames_dropped"] == 0 and stats["corrupt_frames"] == 0
    assert stats["frame_width"] == 960 and stats["frame_height"] == 540

    # --- real detections --------------------------------------------------------------------
    assert stats["detections_total"] >= expected_processed - 10  # ~1 s of frames has no faces
    assert stats["max_faces_in_frame"] == 2
    assert stats["avg_inference_ms"] > 0

    # --- streamed frames carry aligned boxes ------------------------------------------------
    assert "snapshot" in states and "ended" in states
    assert len(frames) >= 5, "the browser stream delivered too few frames"
    checked = 0
    for header, jpeg_len in frames:
        assert jpeg_len > 500
        assert header["session_id"] == sid
        assert header["frame_id"] % (SKIP + 1) == 0  # only detector frames are streamed
        face_free = header["frame_id"] >= TOTAL - FPS * TAIL_SECONDS
        if face_free:
            assert header["faces"] == []
            continue
        want = expected_boxes(header["frame_id"])
        got = [tuple(f["bbox"]) for f in header["faces"]]
        for w in want:
            best = max((iou(g, w) for g in got), default=0.0)
            assert best > 0.5, f"frame {header['frame_id']}: no box overlaps {w} (got {got})"
        checked += 1
    assert checked >= 4

    # --- persistence and cleanup ------------------------------------------------------------
    row = stack.store.get(sid)
    assert row is not None
    assert (row.state, row.active_slot, row.frames_read) == ("COMPLETED", None, TOTAL)
    assert row.detections_total == stats["detections_total"]
    assert stack.get("/sessions/active")["session"] is None
    again = stack.start_session("two")  # worker is free again, no leaked state
    assert again["id"] != sid
    stack.wait_state(again["id"], ("COMPLETED",))


def test_stop_during_playback(stack: Stack) -> None:
    sid = stack.start_session("long")["id"]
    stack.wait_state(sid, ("RUNNING",))
    stack.wait(lambda: stack.session(sid)["stats"]["frames_processed"] > 3, "some frames")
    assert stack.client is not None
    assert stack.client.post(f"/sessions/{sid}/stop").json()["state"] in ("STOPPING", "STOPPED")
    final = stack.wait_state(sid, ("STOPPED", "ERROR"), timeout=15)
    assert final["state"] == "STOPPED"
    assert final["stop_reason"] == "user_requested"
    assert final["stats"]["frames_read"] < 30 * FPS  # stopped early
    assert stack.client.post(f"/sessions/{sid}/stop").json()["state"] == "STOPPED"  # idempotent
    # The worker released everything: it can start the next session immediately.
    nxt = stack.start_session("two")["id"]
    assert stack.wait_state(nxt, ("COMPLETED", "ERROR"))["state"] == "COMPLETED"


def test_unreadable_video_reports_a_clear_error(stack: Stack) -> None:
    sid = stack.start_session("broken")["id"]
    final = stack.wait_state(sid, ("ERROR",))
    assert final["error"]["code"] == "source_invalid"
    assert str(stack.tmp) not in json.dumps(final)  # no server paths leak to the client
    assert stack.get("/sessions/active")["session"] is None


def test_worker_dying_mid_session_fails_the_session(stack: Stack) -> None:
    sid = stack.start_session("long")["id"]
    stack.wait_state(sid, ("RUNNING",))
    assert stack.agent is not None
    stack.agent.request_shutdown()
    final = stack.wait_state(sid, ("ERROR", "STOPPED"), timeout=20)
    assert final["state"] == "ERROR"
    assert final["error"]["code"] == "worker_lost"
    assert stack.get("/sources")["worker"]["connected"] is False
    assert stack.store.active() is None


def test_model_failure_at_start_surfaces_as_session_error(stack: Stack) -> None:
    """The model passes the hello check but fails to load when the session starts."""
    from app.vision.detection.interfaces import ModelUnavailable

    assert stack.agent is not None
    stack.agent.request_shutdown()
    stack.wait(lambda: not stack.get("/sources")["worker"]["connected"], "old worker gone")

    def failing(_s: Settings, _c: Any) -> Any:
        raise ModelUnavailable("The model file could not be loaded (RuntimeException).")

    stack.start_worker(detector_factory=failing)
    sid = stack.start_session("two")["id"]
    final = stack.wait_state(sid, ("ERROR",))
    assert final["error"]["code"] == "model_unavailable"


def test_missing_model_is_reported_before_a_session_is_created(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    s = Stack(tmp_path, sqlite_engine, models_dir=tmp_path / "empty-models")
    s.start()
    try:
        assert s.get("/sources")["worker"]["model_available"] is False
        r = s.client.post("/sessions", json={"source_id": "two"})  # type: ignore[union-attr]
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "model_unavailable"
        assert str(tmp_path) not in r.text  # actionable, but no filesystem paths
        assert s.get("/sessions")["sessions"] == []
    finally:
        s.stop()
