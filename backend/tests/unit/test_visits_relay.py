# ruff: noqa: F811, F401
"""Worker frames/observations -> visit lifecycle -> PostgreSQL-shaped records, events.log, REST API, browser."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select

from app.models import Visit, VisitEvent
from app.workers import protocol as P
from tests.unit.test_recognition_relay import (
    app_of,
    browser_stream,
    observation,
    settle,
    variant,
)
from tests.unit.test_recognition_units import jpeg_bytes
from tests.unit.test_sessions_api import Env, FakeWorker, env, running_session, wait_for


def track(
    tid: int, ms: int, observed: bool = True, state: str = "CONFIRMED", first: int = 0
) -> dict[str, Any]:
    return {
        "track_id": tid,
        "state": state if observed else "LOST",
        "bbox": [10, 10, 60, 60],
        "observed": observed,
        "detection_confidence": 0.9 if observed else None,
        "last_observed_confidence": 0.9,
        "first_seen_ms": first,
        "last_seen_ms": ms,
        "age_ms": ms - first,
        "hits": 5,
        "missed_updates": 0 if observed else 4,
        "last_update_frame": 0,
    }


def frame(sid: str, fid: int, ms: int, *tracks: dict[str, Any]) -> bytes:
    header = {
        "session_id": sid,
        "frame_id": fid,
        "timestamp_ms": ms,
        "frame_width": 320,
        "frame_height": 240,
        "faces": [],
        "inference_ms": 1.0,
        "tracks": list(tracks),
    }
    return P.pack_frame(header, jpeg_bytes())


class Scene:
    """Feeds one visitor (track 1) and later its absence."""

    def __init__(self, env: Env, worker: FakeWorker, sid: str) -> None:
        self.env, self.worker, self.sid = env, worker, sid
        self.fid = 0
        self.ms = 0

    def present(self, seconds: float, tid: int = 1) -> None:
        for _ in range(int(seconds * 10)):
            self.fid += 1
            self.ms += 100
            self.worker.ws.send_bytes(frame(self.sid, self.fid, self.ms, track(tid, self.ms)))

    def absent(self, seconds: float, tid: int = 1) -> None:
        for _ in range(int(seconds)):
            self.fid += 1
            self.ms += 1000
            self.worker.ws.send_bytes(
                frame(self.sid, self.fid, self.ms, track(tid, self.ms, False))
            )

    def recognise(self, tid: int = 1, seeds: int = 5) -> None:
        for seq in range(1, seeds + 1):
            self.worker.ws.send_bytes(observation(self.sid, tid, seq, variant(seq)))

    def events(self, **query: str) -> list[dict[str, Any]]:
        qs = "&".join(f"{k}={v}" for k, v in query.items())
        return self.env.client.get(f"/api/v1/events?{qs}").json()["items"]  # type: ignore[no-any-return]


def scene(env: Env) -> tuple[Scene, FakeWorker]:
    worker = FakeWorker(env.client)
    sid = running_session(env, worker)
    return Scene(env, worker, sid), worker


def entries(s: Scene) -> list[dict[str, Any]]:
    return s.events(type="entry")


def test_presence_opens_one_visit_and_absence_closes_it_with_one_exit(env: Env) -> None:
    s, worker = scene(env)
    with browser_stream(env, s.sid) as browser:
        json.loads(browser.receive_text())
        s.present(1)
        s.recognise()
        s.present(2)
        wait_for(lambda: len(entries(s)) == 1)
        s.absent(10)
        wait_for(lambda: len(s.events(type="exit")) == 1)
        seen = []
        for _ in range(400):
            raw = browser.receive()
            if raw.get("text") is None:  # a video frame
                continue
            m = json.loads(raw["text"])
            if m["type"] == "visit_event":
                seen.append(m["event"])
                if len(seen) == 2:
                    break
    assert [e["type"] for e in seen] == ["entry", "exit"]
    assert all("path" not in json.dumps(e) and "embedding" not in json.dumps(e) for e in seen)

    items = s.events()
    assert sorted(i["type"] for i in items) == ["entry", "exit"]
    entry, exit_ = (next(i for i in items if i["type"] == t) for t in ("entry", "exit"))
    assert entry["visitor"]["label"] == "V00001" == exit_["visitor"]["label"]
    assert entry["visit_id"] == exit_["visit_id"] and entry["visit_id"] != entry["visitor"]["id"]
    assert exit_["exit_basis"] == "absence" and exit_["closure_reason"] == "absence_timeout"
    # exit time = the last sighting, not when the absence was detected
    assert exit_["video_ms"] < s.ms - 5000
    assert entry["image_status"] == "STORED" and exit_["image_status"] == "STORED"
    assert exit_["image_kind"] == "last_good_observation"
    visit = env.client.get(f"/api/v1/visits/{entry['visit_id']}").json()
    assert visit["state"] == "CLOSED" and visit["duration_seconds"] is not None
    assert visit["integrity"] == "complete" and len(visit["events"]) == 2
    worker.close()


def test_replayed_frames_and_observations_do_not_create_more_events(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    s.absent(10)
    wait_for(lambda: len(s.events(type="exit")) == 1)
    before = {i["id"] for i in s.events()}
    # Duplicate delivery of everything the worker already sent.
    for fid in range(1, 40):
        worker.ws.send_bytes(frame(s.sid, fid, fid * 100, track(1, fid * 100)))
    s.recognise()
    worker.send({"type": P.MSG_HEARTBEAT, "active_session_id": s.sid})
    settle(env, "duplicate_observations_ignored", 5)
    assert {i["id"] for i in s.events()} == before
    with app_of(env).state.session_factory() as db:
        assert db.execute(select(func.count()).select_from(Visit)).scalar_one() == 1
    worker.close()


def test_normal_end_of_stream_records_observation_ended_not_a_departure(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    worker.send({"type": P.MSG_ENDED, "session_id": s.sid, "state": "COMPLETED", "stats": {}})
    wait_for(lambda: env.state(s.sid) == "COMPLETED")
    exit_ = s.events(type="exit")
    assert len(exit_) == 1 and exit_[0]["exit_basis"] == "observation_ended"
    assert exit_[0]["closure_reason"] == "end_of_stream"
    visit = env.client.get(f"/api/v1/visits/{exit_[0]['visit_id']}").json()
    assert visit["state"] == "CLOSED"
    worker.close()


def test_manual_stop_ends_the_observation(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    assert env.client.post(f"/api/v1/sessions/{s.sid}/stop").status_code in (200, 202)
    assert worker.recv()["type"] == "session.stop"
    worker.send({"type": P.MSG_ENDED, "session_id": s.sid, "state": "STOPPED", "stats": {}})
    wait_for(lambda: env.state(s.sid) == "STOPPED")
    exit_ = s.events(type="exit")
    assert len(exit_) == 1 and exit_[0]["closure_reason"] == "manual_stop"
    worker.close()


def test_worker_loss_marks_the_visit_interrupted_without_a_duration(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    worker.close()  # the worker process vanishes
    wait_for(lambda: env.state(s.sid) == "ERROR")
    interrupted = s.events(type="interrupted")
    assert len(interrupted) == 1
    assert (
        interrupted[0]["exit_basis"] == "interrupted"
        and interrupted[0]["closure_reason"] == "worker_lost"
    )
    visit = env.client.get(f"/api/v1/visits/{interrupted[0]['visit_id']}").json()
    assert visit["state"] == "INTERRUPTED"
    assert visit["duration_seconds"] is None  # an interrupted visit has no known end


def test_source_error_interrupts_open_visits(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    worker.send(
        {
            "type": P.MSG_ENDED,
            "session_id": s.sid,
            "state": "ERROR",
            "code": "source_lost",
            "message": "rtsp down",
        }
    )
    wait_for(lambda: env.state(s.sid) == "ERROR")
    got = s.events(type="interrupted")
    assert len(got) == 1 and got[0]["closure_reason"] == "source_error"
    worker.close()


def test_a_stalled_worker_does_not_end_the_visit(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    s.ms += 120_000  # two minutes with no frames at all, then a frame without the visitor
    s.fid += 1
    worker.ws.send_bytes(frame(s.sid, s.fid, s.ms, track(1, s.ms, False)))
    worker.send({"type": P.MSG_HEARTBEAT, "active_session_id": s.sid})
    wait_for(lambda: app_of(env).state.visits.manager.stats["blind_gaps"] == 1)
    assert s.events(type="exit") == []
    worker.close()


def test_invalid_timing_and_malformed_tracks_are_ignored(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    bad_time = {
        "session_id": s.sid,
        "frame_id": 99,
        "timestamp_ms": -5,
        "frame_width": 320,
        "frame_height": 240,
        "faces": [],
        "inference_ms": 1,
        "tracks": [track(1, 5)],
    }
    worker.ws.send_bytes(P.pack_frame(bad_time, jpeg_bytes()))
    bad_track = {**bad_time, "timestamp_ms": 9000, "tracks": [{"track_id": 1}]}
    worker.ws.send_bytes(P.pack_frame(bad_track, jpeg_bytes()))
    no_time = {k: v for k, v in bad_time.items() if k != "timestamp_ms"}
    worker.ws.send_bytes(P.pack_frame(no_time, jpeg_bytes()))
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)  # the session is still healthy
    assert env.state(s.sid) == "RUNNING"
    worker.close()


def test_two_visitors_have_independent_visits(env: Env) -> None:
    import numpy as np

    s, worker = scene(env)
    other = np.random.default_rng(99).normal(size=128).astype("float32")
    other /= np.linalg.norm(other)
    for _ in range(10):
        s.fid += 1
        s.ms += 100
        worker.ws.send_bytes(frame(s.sid, s.fid, s.ms, track(1, s.ms), track(2, s.ms)))
    for seq in range(1, 6):
        worker.ws.send_bytes(observation(s.sid, 1, seq, variant(seq)))
        worker.ws.send_bytes(observation(s.sid, 2, seq, other + 0.001 * seq))
    for _ in range(10):
        s.fid += 1
        s.ms += 100
        worker.ws.send_bytes(frame(s.sid, s.fid, s.ms, track(1, s.ms), track(2, s.ms)))
    wait_for(lambda: len(entries(s)) == 2)
    assert {e["visitor"]["label"] for e in entries(s)} == {"V00001", "V00002"}
    assert len({e["visit_id"] for e in entries(s)}) == 2
    worker.close()


def test_late_browser_learns_who_is_inside(env: Env) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    with browser_stream(env, s.sid) as browser:
        assert json.loads(browser.receive_text())["type"] == "snapshot"
        kinds = []
        for _ in range(3):
            m = json.loads(browser.receive_text())
            kinds.append(m["type"])
            if m["type"] == "visits":
                assert m["visits"][0]["state"] == "ACTIVE" and m["visits"][0]["visitor_number"] == 1
                break
    assert "visits" in kinds
    worker.close()


def test_erasing_a_visitor_with_an_open_visit_leaves_an_anonymous_exit_and_no_new_evidence(
    env: Env,
) -> None:
    s, worker = scene(env)
    s.present(1)
    s.recognise()
    s.present(2)
    wait_for(lambda: len(entries(s)) == 1)
    visitor_id = entries(s)[0]["visitor"]["id"]
    store = app_of(env).state.visits.recorder.store
    assert len(store.all_files()) == 1
    assert env.client.delete(f"/api/v1/visitors/{visitor_id}").status_code == 204
    assert store.all_files() == []  # the entry crop is gone with the visitor
    s.absent(10)  # the face is not there any more
    wait_for(lambda: len(s.events(type="exit")) == 1)
    exit_ = s.events(type="exit")[0]
    assert exit_["visitor"] is None and exit_["image_status"] == "ERASED"
    assert store.all_files() == []  # still nothing stored for the erased person
    assert env.client.get(f"/api/v1/events/{exit_['id']}/image").status_code == 404
    worker.close()
