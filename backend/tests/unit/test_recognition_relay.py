# ruff: noqa: F811, F401
"""Worker -> API observations -> identity decisions -> browser identity events (fake worker)."""

from __future__ import annotations

import base64
import concurrent.futures
import contextlib
import json
from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest
from sqlalchemy import func, select

from app.models import IdentityAssociation, Visitor
from app.workers import protocol as P
from tests.unit.test_recognition_units import jpeg_bytes
from tests.unit.test_sessions_api import Env, FakeWorker, env, running_session, wait_for

DIM = 128
PERSON = np.random.default_rng(7).normal(size=DIM).astype(np.float32)
PERSON /= np.linalg.norm(PERSON)


def variant(seed: int, noise: float = 0.15) -> np.ndarray:
    v = PERSON + noise * np.random.default_rng(seed).normal(size=DIM).astype(np.float32) / np.sqrt(
        DIM
    )
    out: np.ndarray = v / np.linalg.norm(v)
    return out


def observation(
    sid: str,
    track: int,
    seq: int,
    vec: np.ndarray | None,
    quality: float = 0.8,
    ts: int | None = None,
    **over: Any,
) -> bytes:
    header: dict[str, Any] = {
        "kind": P.KIND_OBSERVATION,
        "session_id": sid,
        "track_id": track,
        "seq": seq,
        "model_version": "sface-2021dec",
        "dim": DIM,
        "quality": quality,
        "reasons": [] if vec is not None else ["blurry"],
        "embedding_b64": None
        if vec is None
        else base64.b64encode(vec.astype("<f4").tobytes()).decode(),
        "timestamp_ms": seq * 400 if ts is None else ts,
        "frame_id": seq * 3,
    }
    header.update(over)
    return P.pack_frame(header, jpeg_bytes() if vec is not None else b"")


@contextlib.contextmanager
def browser_stream(env: Env, sid: str) -> Iterator[Any]:
    """A browser WebSocket on the session stream.

    Known harness quirk: Starlette's TestClient (one shared portal for every socket) occasionally
    reports the stream handler's own task as cancelled when the test closes the socket. Nothing in
    an assertion or in the application fails; only that specific exception is tolerated on exit."""
    cm = env.client.websocket_connect(f"/api/v1/sessions/{sid}/stream")
    ws = cm.__enter__()
    try:
        yield ws
    finally:
        with contextlib.suppress(concurrent.futures.CancelledError):
            cm.__exit__(None, None, None)


def identity_events(browser: Any, until_kind: str | None, limit: int = 40) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for _ in range(limit):
        message = json.loads(browser.receive_text())
        if message["type"] == "identity":
            events.append(message)
            if message["event"] == until_kind:
                break
    return events


def test_observations_register_a_visitor_and_the_browser_is_told_once(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with browser_stream(env, sid) as browser:
        json.loads(browser.receive_text())  # snapshot
        for seq in range(1, 6):
            worker.ws.send_bytes(observation(sid, 3, seq, variant(seq)))
        events = identity_events(browser, "registered")
        settle(env, "observations", 5)
        registered = events[-1]
        assert registered["event"] == "registered" and registered["state"] == "REGISTERED"
        assert (
            registered["track_id"] == 3
        )  # the track id travels with the (separate) visitor identity
        assert registered["visitor"]["label"] == "V00001" and registered["visitor"]["id"] != 3
        assert "embedding" not in json.dumps(registered).lower()
    with app_of(env).state.session_factory() as db:
        assert db.execute(select(func.count()).select_from(Visitor)).scalar_one() == 1
    worker.close()


def test_the_session_settings_say_whether_recognition_is_active(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    settings = env.get(sid)["settings"]
    assert settings["recognition_enabled"] is True and settings["tracking_enabled"] is True
    worker.close()


def test_a_second_session_recognises_the_same_face_without_registering_again(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    for seq in range(1, 6):
        worker.ws.send_bytes(observation(sid, 1, seq, variant(seq)))
    wait_for(lambda: _visitors(env) == 1)
    worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "COMPLETED", "stats": {}})
    wait_for(lambda: env.state(sid) == "COMPLETED")

    sid2 = running_session(env, worker)
    with browser_stream(env, sid2) as browser:
        json.loads(browser.receive_text())
        for seq in range(1, 5):
            worker.ws.send_bytes(observation(sid2, 9, seq, variant(50 + seq)))
        events = identity_events(browser, "recognized")
        settle(env, "observations", 9)
    assert events[-1]["event"] == "recognized" and events[-1]["visitor"]["label"] == "V00001"
    assert events[-1]["track_id"] == 9  # a different track id in a different session: same visitor
    assert _visitors(env) == 1
    worker.close()


def test_insufficient_quality_is_reported_once_and_registers_nothing(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with browser_stream(env, sid) as browser:
        json.loads(browser.receive_text())
        for seq in range(1, 8):
            worker.ws.send_bytes(observation(sid, 2, seq, None))
        events = identity_events(browser, "state", limit=3)
        settle(env, "insufficient_quality", 7)
    assert [e["state"] for e in events] == ["INSUFFICIENT_QUALITY"]
    assert events[0]["reason"] == "blurry" and events[0]["visitor"] is None
    wait_for(lambda: app_of(env).state.identity.runtime_stats()["insufficient_quality"] == 7)
    assert _visitors(env) == 0
    worker.close()


@pytest.mark.parametrize(
    "bad",
    [
        {"embedding_b64": "###"},
        {"quality": 7},
        {"track_id": -3},
        {"model_version": "other-model"},  # incompatible model: never compared
        {"session_id": "f" * 32},  # a session that is not running (delayed / stray message)
    ],
)
def test_invalid_or_foreign_observations_are_dropped_and_the_stream_survives(
    env: Env, bad: dict[str, Any]
) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    with browser_stream(env, sid) as browser:
        json.loads(browser.receive_text())
        for seq in range(1, 6):
            worker.ws.send_bytes(observation(sid, 1, seq, variant(seq), **bad))
        for seq in range(1, 6):  # the good ones still work afterwards
            worker.ws.send_bytes(observation(sid, 2, seq, variant(10 + seq)))
        events = identity_events(browser, "registered")
        settle(env, "usable_observations", 5)
    assert events[-1]["track_id"] == 2 and _visitors(env) == 1
    worker.close()


def test_a_browser_that_connects_later_receives_the_current_identities(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    for seq in range(1, 6):
        worker.ws.send_bytes(observation(sid, 5, seq, variant(seq)))
    wait_for(lambda: _visitors(env) == 1)
    settle(env, "observations", 5)
    with browser_stream(env, sid) as browser:
        assert json.loads(browser.receive_text())["type"] == "snapshot"
        late = json.loads(browser.receive_text())
    assert late["type"] == "identities"
    assert late["identities"][0]["track_id"] == 5 and late["identities"][0]["state"] == "REGISTERED"
    assert late["identities"][0]["visitor"]["label"] == "V00001"
    worker.close()


def test_observations_after_the_session_ended_are_ignored(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    worker.send({"type": P.MSG_ENDED, "session_id": sid, "state": "COMPLETED", "stats": {}})
    wait_for(lambda: env.state(sid) == "COMPLETED")
    for seq in range(1, 8):  # delayed worker messages arrive after the end
        worker.ws.send_bytes(observation(sid, 1, seq, variant(seq)))
    # a barrier message: once the API handled it, the delayed ones were handled too
    worker.send({"type": P.MSG_HEARTBEAT, "active_session_id": None})
    wait_for(lambda: True)
    assert _visitors(env) == 0
    worker.close()


def test_associations_are_stored_when_the_session_ends(env: Env) -> None:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    for seq in range(1, 6):
        worker.ws.send_bytes(observation(sid, 4, seq, variant(seq)))
    wait_for(lambda: _visitors(env) == 1)
    settle(env, "observations", 5)
    worker.send(
        {
            "type": P.MSG_ENDED,
            "session_id": sid,
            "state": "COMPLETED",
            "stats": {"recog_enabled": 1},
        }
    )
    wait_for(lambda: env.state(sid) == "COMPLETED")
    with app_of(env).state.session_factory() as db:
        row = db.execute(select(IdentityAssociation)).scalar_one()
    assert (row.session_id, row.track_id, row.state, row.registered_here) == (
        sid,
        4,
        "REGISTERED",
        1,
    )
    assert env.get(sid)["stats"]["recog_enabled"] == 1
    worker.close()


def app_of(env: Env) -> Any:
    return env.client.app


def settle(env: Env, counter: str, at_least: int) -> None:
    """Wait until the API has processed what the fake worker sent.

    Closing a browser socket while the worker socket is still being served in a thread can surface
    a CancelledError from Starlette's shared test portal (a harness artefact; uvicorn connections
    are independent), so tests wait for the counters instead of racing the teardown."""
    wait_for(lambda: app_of(env).state.identity.runtime_stats().get(counter, 0) >= at_least)


def _visitors(env: Env) -> int:
    with app_of(env).state.session_factory() as db:
        return int(db.execute(select(func.count()).select_from(Visitor)).scalar_one())
