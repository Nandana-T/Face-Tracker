"""Session lifecycle through the real API + worker WebSocket, with a scripted fake worker.

The DB is in-memory SQLite (session logic only); PostgreSQL behaviour of the same schema is covered
by tests/integration. The fake worker proves the API contract - not detection quality.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine
from sqlalchemy.orm import sessionmaker
from starlette.websockets import WebSocketDisconnect

from app.main import create_app
from app.services.session_store import (
    ActiveSessionExists,
    InvalidTransition,
    SessionStore,
    can_transition,
)
from app.workers import protocol as P
from tests.conftest import make_full_config, make_settings
from tests.unit.test_sources import write_video

TOKEN = "unit-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@contextlib.contextmanager
def api(app: Any) -> Iterator[TestClient]:
    """TestClient that closes every fake worker on exit - even when an assertion failed.

    Otherwise the still-open worker socket blocks the app shutdown and the test run hangs.
    """
    with TestClient(app) as client:
        client.fake_workers = []
        try:
            yield client
        finally:
            for w in client.fake_workers:
                w.close()


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


class FakeWorker:
    """Speaks the worker side of the protocol over the real WebSocket endpoint."""

    def __init__(self, client: TestClient, model_ok: bool = True) -> None:
        self.client = client
        self._closed = False
        getattr(client, "fake_workers", []).append(self)
        self._cm = client.websocket_connect("/api/v1/worker/ws", headers=AUTH)
        self.ws = self._cm.__enter__()
        self.send(
            {
                "type": P.MSG_HELLO,
                "worker_id": "fake-1",
                "model": {
                    "key": "m",
                    "available": model_ok,
                    "detail": None if model_ok else "no model",
                },
            }
        )
        wait_for(
            lambda: client.get("/api/v1/sources").json()["worker"]["model_available"] is not None
        )

    def send(self, message: dict[str, Any]) -> None:
        self.ws.send_text(json.dumps(message))

    def recv(self) -> dict[str, Any]:
        return json.loads(self.ws.receive_text())  # type: ignore[no-any-return]

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._cm.__exit__(None, None, None)


def jpeg_frame(session_id: str, frame_id: int = 0) -> bytes:
    header = {
        "session_id": session_id,
        "frame_id": frame_id,
        "timestamp_ms": 0,
        "frame_width": 64,
        "frame_height": 48,
        "faces": [{"bbox": [1, 2, 30, 40], "confidence": 0.9}],
        "inference_ms": 1.0,
    }
    return P.pack_frame(header, b"\xff\xd8\xff\xe0fakejpeg")


class Env:
    def __init__(self, client: TestClient, engine: Engine) -> None:
        self.client = client
        self.engine = engine
        self.store = SessionStore(sessionmaker(bind=engine, expire_on_commit=False))

    def start(self, source_id: str = "clip") -> Any:
        return self.client.post("/api/v1/sessions", json={"source_id": source_id})

    def get(self, sid: str) -> dict[str, Any]:
        return self.client.get(f"/api/v1/sessions/{sid}").json()  # type: ignore[no-any-return]

    def state(self, sid: str) -> str:
        state: str = self.get(sid)["state"]
        return state


def build(tmp_path: Path, engine: Engine, worker_cfg: dict[str, Any] | None = None) -> Any:
    video = write_video(tmp_path / "clip.mp4")
    cfg = make_full_config(
        tmp_path,
        video={
            "sample_dir": str(tmp_path / "none"),
            "sources": [
                {"id": "clip", "label": "Clip", "type": "file", "path": str(video)},
                {"id": "other", "label": "Other", "type": "file", "path": str(video)},
                {"id": "gone", "label": "Gone", "type": "file", "path": str(tmp_path / "x.mp4")},
            ],
        },
        worker=worker_cfg or {},
    )
    return create_app(
        settings=make_settings(tmp_path, worker_token=SecretStr(TOKEN)), config=cfg, engine=engine
    )


@pytest.fixture
def env(tmp_path: Path, sqlite_engine: Engine) -> Iterator[Env]:
    with api(build(tmp_path, sqlite_engine)) as client:
        yield Env(client, sqlite_engine)


def running_session(env: Env, worker: FakeWorker) -> str:
    r = env.start()
    assert r.status_code == 202
    sid: str = r.json()["id"]
    assert worker.recv() == {"type": "session.start", "session_id": sid, "source_id": "clip"}
    worker.send({"type": P.MSG_ACCEPTED, "session_id": sid})
    worker.send({"type": P.MSG_STATE, "session_id": sid, "state": "running"})
    wait_for(lambda: env.state(sid) == "RUNNING")
    return sid


# ---- sources / preconditions ----------------------------------------------------------------
def test_sources_listing_hides_server_details(env: Env, tmp_path: Path) -> None:
    body = env.client.get("/api/v1/sources").json()
    assert {s["id"] for s in body["sources"]} == {"clip", "other", "gone"}
    assert body["worker"]["connected"] is False
    assert str(tmp_path) not in json.dumps(body)
    assert env.client.get("/api/v1/sources/clip").json()["type"] == "file"
    assert env.client.get("/api/v1/sources/missing").status_code == 404


def test_start_requires_a_worker(env: Env) -> None:
    r = env.start()
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "worker_unavailable"
    assert env.client.get("/api/v1/sessions").json()["sessions"] == []  # nothing was persisted


def test_start_rejects_bad_input(env: Env) -> None:
    worker = FakeWorker(env.client)
    assert env.start("nope").status_code == 404
    unavailable = env.start("gone")
    assert (unavailable.status_code, unavailable.json()["error"]["code"]) == (
        409,
        "source_unavailable",
    )
    for bad in ("../etc/passwd", "C:\\video.mp4", "", "A" * 80):
        assert env.client.post("/api/v1/sessions", json={"source_id": bad}).status_code == 422
    assert (
        env.client.post("/api/v1/sessions", json={"source_id": "clip", "path": "/x"}).status_code
        == 422
    )
    worker.close()


def test_model_unavailable_is_reported_before_any_session(env: Env) -> None:
    worker = FakeWorker(env.client, model_ok=False)
    r = env.start()
    assert (r.status_code, r.json()["error"]["code"]) == (503, "model_unavailable")
    assert env.client.get("/api/v1/sessions").json()["sessions"] == []
    status = env.client.get("/api/v1/sources").json()["worker"]
    assert status["model_available"] is False
    worker.close()


# ---- lifecycle -------------------------------------------------------------------------------
def test_full_lifecycle_to_completion(env: Env) -> None:
    worker = FakeWorker(env.client)
    r = env.start()
    sid = r.json()["id"]
    assert r.json()["state"] == "STARTING"  # not RUNNING until the worker confirms
    assert env.client.get("/api/v1/sessions/active").json()["session"]["id"] == sid
    worker.recv()
    worker.send({"type": P.MSG_ACCEPTED, "session_id": sid})
    assert env.state(sid) == "STARTING"
    worker.send({"type": P.MSG_STATE, "session_id": sid, "state": "running"})
    wait_for(lambda: env.state(sid) == "RUNNING")
    stats = {
        "frames_read": 30,
        "frames_processed": 10,
        "frames_skipped": 20,
        "detections_total": 7,
        "faces_last_frame": 1,
        "processing_fps": 8.5,
        "avg_inference_ms": 22.0,
    }
    worker.send({"type": P.MSG_STATS, "session_id": sid, "stats": stats})
    wait_for(lambda: env.get(sid)["stats"]["frames_processed"] == 10)
    assert env.client.get(f"/api/v1/sessions/{sid}/stats").json()["detections_total"] == 7
    worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "COMPLETED", "stats": stats})
    wait_for(lambda: env.state(sid) == "COMPLETED")
    final = env.get(sid)
    assert final["ended_at"] and final["started_at"]
    assert final["stop_reason"] == "end_of_stream"
    assert final["stats"]["frames_read"] == 30
    assert env.client.get("/api/v1/sessions/active").json()["session"] is None
    row = env.store.get(sid)
    assert row is not None
    assert (row.active_slot, row.frames_processed, row.detections_total) == (None, 10, 7)
    assert env.start().status_code == 202  # the slot was released
    worker.close()


def test_repeated_start_is_idempotent_and_conflicting_start_is_refused(env: Env) -> None:
    worker = FakeWorker(env.client)
    first = env.start()
    again = env.start()
    assert first.status_code == 202
    assert again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    other = env.start("other")
    assert (other.status_code, other.json()["error"]["code"]) == (409, "session_active")
    assert other.json()["error"]["details"]["session_id"] == first.json()["id"]
    assert len(env.client.get("/api/v1/sessions").json()["sessions"]) == 1
    worker.close()


def test_competing_start_requests_create_exactly_one_session(env: Env) -> None:
    worker = FakeWorker(env.client)
    results: list[tuple[int, str | None]] = []
    barrier = threading.Barrier(6)

    def go(source_id: str) -> None:
        barrier.wait()
        r = env.start(source_id)
        results.append((r.status_code, r.json().get("id")))

    threads = [threading.Thread(target=go, args=("clip" if i % 2 else "other",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    created = [r for r in results if r[0] == 202]
    assert len(created) == 1
    assert len(env.client.get("/api/v1/sessions").json()["sessions"]) == 1
    worker.close()


def test_stop_flow_and_idempotence(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    r = env.client.post(f"/api/v1/sessions/{sid}/stop")
    assert (r.status_code, r.json()["state"]) == (200, "STOPPING")
    assert worker.recv() == {"type": "session.stop", "session_id": sid}
    again = env.client.post(f"/api/v1/sessions/{sid}/stop")
    assert again.json()["state"] == "STOPPING"  # no second command, no error
    worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "STOPPED", "stats": {}})
    wait_for(lambda: env.state(sid) == "STOPPED")
    done = env.client.post(f"/api/v1/sessions/{sid}/stop")
    assert (done.status_code, done.json()["state"]) == (200, "STOPPED")
    assert env.get(sid)["stop_reason"] == "user_requested"
    worker.close()


def test_stop_unknown_session_is_404(env: Env) -> None:
    assert env.client.post("/api/v1/sessions/" + "0" * 32 + "/stop").status_code == 404
    assert env.client.get("/api/v1/sessions/" + "0" * 32).status_code == 404


def test_stop_timeout_forces_stopped(tmp_path: Path, sqlite_engine: Engine) -> None:
    app = build(tmp_path, sqlite_engine, {"stop_timeout_seconds": 0.3})
    with api(app) as client:
        e = Env(client, sqlite_engine)
        worker = FakeWorker(client)
        sid = running_session(e, worker)
        client.post(f"/api/v1/sessions/{sid}/stop")
        wait_for(lambda: e.state(sid) == "STOPPED", timeout=3)  # worker never answered
        assert e.get(sid)["stop_reason"] == "stop_timeout"
        worker.close()


def test_start_timeout_becomes_error(tmp_path: Path, sqlite_engine: Engine) -> None:
    app = build(tmp_path, sqlite_engine, {"start_timeout_seconds": 0.3})
    with api(app) as client:
        e = Env(client, sqlite_engine)
        worker = FakeWorker(client)
        sid = e.start().json()["id"]
        worker.recv()
        wait_for(lambda: e.state(sid) == "ERROR", timeout=3)  # worker never confirmed RUNNING
        assert e.get(sid)["error"]["code"] == "start_timeout"
        assert worker.recv()["type"] == "session.stop"  # and the worker is told to give up
        worker.close()


def test_worker_error_is_propagated_to_the_session(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = env.start().json()["id"]
    worker.recv()
    worker.send(
        {
            "type": P.MSG_ENDED,
            "session_id": sid,
            "state": "ERROR",
            "code": "camera_unavailable",
            "message": "The camera could not be reached.",
            "stats": {},
        }
    )
    wait_for(lambda: env.state(sid) == "ERROR")
    err = env.get(sid)["error"]
    assert err == {"code": "camera_unavailable", "message": "The camera could not be reached."}
    assert env.client.get("/api/v1/sessions/active").json()["session"] is None
    worker.close()


def test_worker_disconnect_during_run_is_an_error(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    worker.close()
    wait_for(lambda: env.state(sid) == "ERROR")
    assert env.get(sid)["error"]["code"] == "worker_lost"
    assert env.client.get("/api/v1/sources").json()["worker"]["connected"] is False
    assert env.store.get(sid).active_slot is None  # type: ignore[union-attr]


def test_worker_disconnect_during_stop_completes_the_stop(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    env.client.post(f"/api/v1/sessions/{sid}/stop")
    worker.close()
    wait_for(lambda: env.state(sid) == "STOPPED")


def test_silent_worker_is_declared_lost(tmp_path: Path, sqlite_engine: Engine) -> None:
    app = build(tmp_path, sqlite_engine, {"heartbeat_interval_seconds": 0.1})
    with api(app) as client:
        e = Env(client, sqlite_engine)
        worker = FakeWorker(client)
        sid = running_session(e, worker)
        wait_for(lambda: e.state(sid) == "ERROR", timeout=5)  # no heartbeats were sent
        assert e.get(sid)["error"]["code"] == "worker_lost"
        worker.close()


def test_worker_disowning_the_session_is_an_error(tmp_path: Path, sqlite_engine: Engine) -> None:
    app = build(tmp_path, sqlite_engine, {"heartbeat_interval_seconds": 0.2})
    with api(app) as client:
        e = Env(client, sqlite_engine)
        worker = FakeWorker(client)
        sid = running_session(e, worker)
        # A healthy worker heartbeats, but reports that it is not processing anything.
        deadline = time.monotonic() + 5
        while e.state(sid) != "ERROR" and time.monotonic() < deadline:
            worker.send({"type": P.MSG_HEARTBEAT, "active_session_id": None})
            time.sleep(0.1)
        assert e.get(sid)["error"]["code"] == "worker_lost_session"
        worker.close()


def test_state_reports_source_reconnecting(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    worker.send({"type": P.MSG_STATE, "session_id": sid, "state": "reconnecting"})
    wait_for(lambda: env.get(sid)["connection"] == "reconnecting")
    worker.send({"type": P.MSG_STATE, "session_id": sid, "state": "connected"})
    wait_for(lambda: env.get(sid)["connection"] == "connected")
    worker.close()


def test_messages_for_other_sessions_are_ignored(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    worker.send({"type": P.MSG_ENDED, "session_id": "f" * 32, "state": "ERROR", "code": "x"})
    time.sleep(0.2)
    assert env.state(sid) == "RUNNING"
    worker.close()


# ---- worker authentication ------------------------------------------------------------------
@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}]
)
def test_worker_socket_requires_the_token(env: Env, headers: dict[str, str]) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        env.client.websocket_connect("/api/v1/worker/ws", headers=headers),
    ):
        pass
    assert env.client.get("/api/v1/sources").json()["worker"]["connected"] is False


def test_worker_socket_refused_when_no_token_configured(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    app = create_app(
        settings=make_settings(tmp_path), config=make_full_config(tmp_path), engine=sqlite_engine
    )
    with (
        TestClient(app) as client,
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/v1/worker/ws", headers=AUTH),
    ):
        pass


# ---- browser stream --------------------------------------------------------------------------
def test_browser_receives_snapshot_frames_stats_and_end(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        snap = json.loads(browser.receive_text())
        assert snap["type"] == "snapshot"
        assert snap["session"]["state"] == "RUNNING"
        worker.ws.send_bytes(jpeg_frame("f" * 32))  # wrong session: must never reach the browser
        worker.ws.send_bytes(b"garbage")  # malformed: dropped
        worker.ws.send_bytes(jpeg_frame(sid, 7))
        header, jpeg = P.unpack_frame(browser.receive_bytes())
        assert header["frame_id"] == 7
        assert header["faces"][0]["bbox"] == [1, 2, 30, 40]
        assert jpeg.startswith(b"\xff\xd8")
        worker.send({"type": P.MSG_STATS, "session_id": sid, "stats": {"frames_read": 5}})
        assert json.loads(browser.receive_text())["type"] == "stats"
        worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "COMPLETED", "stats": {}})
        ended = json.loads(browser.receive_text())
        assert ended["type"] == "ended"
        assert ended["session"]["state"] == "COMPLETED"
    worker.close()


def test_all_frames_arrive_in_order_when_the_browser_keeps_up(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        browser.receive_text()
        for i in range(20):
            worker.ws.send_bytes(jpeg_frame(sid, i))
        worker.ws.send_bytes(jpeg_frame(sid, 999))
        ids: list[int] = []
        while not ids or ids[-1] != 999:
            ids.append(P.unpack_frame(browser.receive_bytes())[0]["frame_id"])
        assert ids == sorted(ids)  # never reordered; may be thinned but never duplicated
        assert len(set(ids)) == len(ids)
    worker.close()


async def test_slow_subscriber_keeps_only_the_latest_frame() -> None:
    from app.services.session_manager import Subscriber

    sub = Subscriber()
    for i in range(100):
        sub.push_frame(f"frame-{i}".encode())
    controls, frame = await sub.next_batch()
    assert frame == b"frame-99"  # intermediate frames were discarded, memory is O(1)
    assert controls == []
    assert sub.dropped_frames == 99


async def test_control_queue_is_bounded() -> None:
    from app.services.session_manager import Subscriber

    sub = Subscriber()
    for i in range(500):
        sub.push_control(f"m{i}")
    controls, _ = await sub.next_batch()
    assert len(controls) == 100
    assert controls[-1] == "m499"  # newest kept, oldest dropped


def test_stream_for_finished_session_sends_snapshot_and_closes(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = env.start().json()["id"]
    worker.recv()
    worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "COMPLETED", "stats": {}})
    wait_for(lambda: env.state(sid) == "COMPLETED")
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        assert json.loads(browser.receive_text())["session"]["state"] == "COMPLETED"
        with pytest.raises(WebSocketDisconnect):
            browser.receive_text()
    worker.close()


def test_stream_rejects_unknown_session_and_foreign_origin(env: Env) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as unknown,
        env.client.websocket_connect("/api/v1/sessions/" + "0" * 32 + "/stream"),
    ):
        pass
    assert unknown.value.code == 4404
    with (
        pytest.raises(WebSocketDisconnect),
        env.client.websocket_connect(
            "/api/v1/sessions/" + "0" * 32 + "/stream", headers={"Origin": "https://evil.example"}
        ),
    ):
        pass


def test_subscriber_is_released_on_disconnect(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    mgr = env.client.app.state.sessions  # type: ignore[attr-defined]
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        browser.receive_text()
        wait_for(lambda: len(mgr.live.subscribers) == 1)
    wait_for(lambda: len(mgr.live.subscribers) == 0)
    worker.close()


# ---- persistence / consistency ---------------------------------------------------------------
def test_orphaned_sessions_are_failed_at_startup(tmp_path: Path, sqlite_engine: Engine) -> None:
    store = SessionStore(sessionmaker(bind=sqlite_engine, expire_on_commit=False))
    row = store.create_active("clip", "Clip", "file", "w", {})
    with api(build(tmp_path, sqlite_engine)) as client:
        body = client.get(f"/api/v1/sessions/{row.id}").json()
        assert body["state"] == "ERROR"
        assert body["error"]["code"] == "api_restarted"
        assert client.get("/api/v1/sessions/active").json()["session"] is None
    assert store.get(row.id).active_slot is None  # type: ignore[union-attr]


def test_database_allows_only_one_active_session(sqlite_engine: Engine) -> None:
    store = SessionStore(sessionmaker(bind=sqlite_engine, expire_on_commit=False))
    first = store.create_active("a", "A", "file", None, {})
    with pytest.raises(ActiveSessionExists):
        store.create_active("b", "B", "file", None, {})
    store.transition(first.id, "RUNNING")
    store.transition(first.id, "COMPLETED")
    assert store.create_active("b", "B", "file", None, {}).state == "STARTING"


def test_state_machine() -> None:
    assert can_transition("STARTING", "RUNNING")
    assert can_transition("RUNNING", "STOPPING")
    assert can_transition("STOPPING", "STOPPED")
    for terminal in ("STOPPED", "COMPLETED", "ERROR"):
        assert not any(
            can_transition(terminal, n) for n in ("STARTING", "RUNNING", "STOPPING", "STOPPED")
        )
    assert not can_transition("RUNNING", "STARTING")
    assert not can_transition("STOPPING", "RUNNING")


def test_terminal_session_cannot_be_revived(sqlite_engine: Engine) -> None:
    store = SessionStore(sessionmaker(bind=sqlite_engine, expire_on_commit=False))
    row = store.create_active("a", "A", "file", None, {})
    store.transition(row.id, "ERROR", error_code="x", error_message="boom")
    with pytest.raises(InvalidTransition):
        store.transition(row.id, "RUNNING")
    fresh = store.get(row.id)
    assert fresh is not None
    assert (fresh.state, fresh.ended_at is not None) == ("ERROR", True)


def test_api_shutdown_fails_active_sessions(tmp_path: Path, sqlite_engine: Engine) -> None:
    app = build(tmp_path, sqlite_engine)
    with api(app) as client:
        e = Env(client, sqlite_engine)
        worker = FakeWorker(client)
        sid = e.start().json()["id"]
        worker.recv()
        worker.close()
    row = SessionStore(sessionmaker(bind=sqlite_engine, expire_on_commit=False)).get(sid)
    assert row is not None
    assert row.state == "ERROR"
    assert row.active_slot is None


# ---- Phase 2: tracking metadata ----------------------------------------------------------------
def tracked_frame(session_id: str, frame_id: int, tracks: list[dict[str, Any]]) -> bytes:
    header = {
        "session_id": session_id,
        "frame_id": frame_id,
        "timestamp_ms": frame_id * 40,
        "frame_width": 64,
        "frame_height": 48,
        "faces": [{"bbox": [1, 2, 30, 40], "confidence": 0.9}],
        "inference_ms": 1.0,
        "tracks": tracks,
        "tracking": {
            "enabled": True,
            "tentative": 0,
            "confirmed": len(tracks),
            "lost": 0,
            "created_total": len(tracks),
            "confirmed_total": len(tracks),
            "tracking_ms": 0.2,
        },
    }
    return P.pack_frame(header, b"\xff\xd8\xff\xe0fakejpeg")


def track(track_id: int = 1, **over: Any) -> dict[str, Any]:
    base = {
        "track_id": track_id,
        "state": "CONFIRMED",
        "bbox": [1, 2, 30, 40],
        "observed": True,
        "detection_confidence": 0.9,
        "last_observed_confidence": 0.9,
        "first_seen_ms": 0,
        "last_seen_ms": 40,
        "age_ms": 40,
        "hits": 3,
        "missed_updates": 0,
        "last_update_frame": 1,
    }
    return {**base, **over}


def test_valid_track_metadata_reaches_the_browser_with_the_session_id(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        browser.receive_text()
        worker.ws.send_bytes(tracked_frame(sid, 1, [track(7)]))
        header, _ = P.unpack_frame(browser.receive_bytes())
        assert header["session_id"] == sid
        assert header["tracks"][0]["track_id"] == 7 and header["tracking"]["confirmed"] == 1
    worker.close()


@pytest.mark.parametrize(
    "bad_tracks",
    [
        [track(1, bbox=[0, 0, 500, 500])],  # outside the 64x48 frame
        [track(1), track(1)],  # duplicate id
        [track(1, observed=False)],  # predicted box claiming a confidence
        [track(1, observed=True, detection_confidence=None)],  # observed without confidence
        [track(1, state="REMOVED")],
        [track(0)],
        [track(1, detection_confidence=1.5)],
    ],
)
def test_malformed_track_metadata_is_dropped_and_the_stream_continues(
    env: Env, bad_tracks: list[dict[str, Any]]
) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        browser.receive_text()
        worker.ws.send_bytes(tracked_frame(sid, 1, bad_tracks))
        worker.ws.send_bytes(tracked_frame(sid, 2, [track(1)]))
        header, _ = P.unpack_frame(browser.receive_bytes())
        assert header["frame_id"] == 2  # the invalid frame never reached the browser
    worker.close()


def test_tracks_of_another_session_never_reach_the_browser(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream") as browser:
        browser.receive_text()
        worker.ws.send_bytes(tracked_frame("e" * 32, 1, [track(1)]))
        worker.ws.send_bytes(tracked_frame(sid, 2, [track(9)]))
        header, _ = P.unpack_frame(browser.receive_bytes())
        assert header["session_id"] == sid and header["tracks"][0]["track_id"] == 9
    worker.close()


def test_tracking_counters_are_persisted_in_the_session_summary(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    stats = {
        "frames_read": 90,
        "frames_processed": 30,
        "tracks_confirmed": 2,
        "tracks_created_total": 5,
        "tracks_confirmed_total": 3,
        "tracks_rejected_total": 2,
        "tracks_removed_total": 1,
        "tracking_ms_avg": 0.4,
    }
    worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "COMPLETED", "stats": stats})
    wait_for(lambda: env.state(sid) == "COMPLETED")
    body = env.get(sid)
    assert body["stats"]["tracks_created_total"] == 5
    assert body["stats"]["tracks_confirmed_total"] == 3
    assert body["stats"]["tracks_rejected_total"] == 2
    worker.close()
