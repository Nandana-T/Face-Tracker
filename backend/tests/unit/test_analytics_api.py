# ruff: noqa: F811, F401
"""Analytics REST + change stream: auth, validation, definitions, privacy, live notifications."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.unit.test_events_api import BASE, seed_visits
from tests.unit.test_recognition_relay import app_of
from tests.unit.test_sessions_api import Env, env, wait_for
from tests.unit.test_visitors_api import Stack, stack
from tests.unit.test_visits_relay import entries, scene

ROUTES = [
    ("GET", "/api/v1/analytics/overview"),
    ("GET", "/api/v1/analytics/timeline"),
    ("GET", "/api/v1/analytics/sources"),
    ("GET", "/api/v1/analytics/sessions"),
    ("GET", "/api/v1/analytics/coverage"),
    ("GET", "/api/v1/analytics/recent"),
    ("GET", "/api/v1/analytics/reconciliation"),
    ("POST", "/api/v1/analytics/stream-ticket"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_analytics_route_requires_a_token(stack: Stack, method: str, path: str) -> None:
    r = TestClient(stack.app).request(method, path)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_empty_database_gives_zeros_not_errors_or_invented_numbers(stack: Stack) -> None:
    o = stack.get("/analytics/overview").json()
    assert o["registered_visitors"] == 0 and o["unique_visitors_in_scope"] == 0 and o["visits"] == 0
    assert o["active_visits"] == 0 and o["last_event_recorded_at"] is None
    assert stack.get("/analytics/timeline").json()["points"] == []
    assert stack.get("/analytics/sources").json()["items"] == []
    assert stack.get("/analytics/recent").json()["items"] == []
    assert stack.get("/analytics/reconciliation").json()["consistent"] is True


def test_overview_counts_distinct_visitors_and_keeps_visits_separate(stack: Stack) -> None:
    seed_visits(stack, 4, interrupted=(3,), open_=(4,))
    o = stack.get("/analytics/overview").json()
    assert (
        o["registered_visitors"],
        o["unique_visitors_in_scope"],
        o["visits"],
        o["entry_events"],
    ) == (4, 4, 4, 4)
    assert o["terminal_events"] == 3 and o["terminal_by_basis"] == {
        "absence": 2,
        "observation_ended": 0,
        "interrupted": 1,
    }
    assert (
        o["active_visits"] == 0 and o["stale_open_visits"] == 1
    )  # the open visit's session is not live
    assert "unique_visitors_in_scope" in o["definitions"]
    assert stack.get("/analytics/reconciliation").json()["consistent"] is True


def test_filters_by_range_source_and_session_are_consistent_across_endpoints(stack: Stack) -> None:
    seed_visits(stack, 4)
    since = (BASE + timedelta(hours=2, minutes=30)).isoformat().replace("+00:00", "Z")
    params = {"since": since}
    assert stack.get("/analytics/overview", params=params).json()["visits"] == 2
    tl = stack.get("/analytics/timeline", params={**params, "bucket": "hour"}).json()
    assert (
        sum(p["entries"] for p in tl["points"]) == 2
        and tl["bucket"] == "hour"
        and tl["timezone"] == "UTC"
    )
    assert stack.get("/analytics/sources", params=params).json()["items"][0]["visits"] == 2
    assert len(stack.get("/analytics/recent", params={**params, "limit": 10}).json()["items"]) == 4
    assert stack.get("/analytics/overview", params={"source_id": "nowhere"}).json()["visits"] == 0
    assert (
        stack.get("/analytics/overview", params={"session_id": "a" * 32}).json()["visits"] == 0
    )  # (real ids are uuid hex; the seeded one is not)


@pytest.mark.parametrize(
    "query",
    [
        "since=2026-02-02T00:00:00Z&until=2026-02-01T00:00:00Z",  # inverted
        "since=2026-02-01T00:00:00Z&until=2026-02-01T00:00:00Z",  # empty
        "since=notadate",
        "session_id=xyz",
        "source_id=../etc",
        "bucket=week",
    ],
)
def test_invalid_parameters_are_rejected(stack: Stack, query: str) -> None:
    assert stack.get(f"/analytics/timeline?{query}").status_code == 422
    assert stack.get(f"/analytics/overview?{query}").status_code in (
        200,
        422,
    )  # `bucket` is timeline-only


def test_sessions_and_pagination(stack: Stack) -> None:
    seed_visits(stack, 3)
    page = stack.get("/analytics/sessions?limit=1").json()
    assert page["limit"] == 1 and page["total"] == 1 and page["items"][0]["visits"] == 3
    assert stack.get("/analytics/sessions?limit=0").status_code == 422
    assert stack.get("/analytics/sessions?limit=101").status_code == 422


def test_coverage_carries_its_disclaimer_and_no_visitor_inflation(stack: Stack) -> None:
    seed_visits(stack, 2)
    c = stack.get("/analytics/coverage").json()
    assert "not person-level recall" in c["disclaimer"]
    assert stack.get("/analytics/overview").json()["unique_visitors_in_scope"] == 2


def test_no_paths_embeddings_or_secrets_in_any_analytics_response(
    stack: Stack, tmp_path: Any
) -> None:
    seed_visits(stack, 3)
    blob = json.dumps(
        [
            stack.get(p).json()
            for p in (
                "/analytics/overview",
                "/analytics/timeline",
                "/analytics/sources",
                "/analytics/sessions",
                "/analytics/coverage",
                "/analytics/recent",
                "/analytics/reconciliation",
            )
        ]
    ).lower()
    for forbidden in (
        ".jpg",
        "image_path",
        "evidence_dir",
        "entries/",
        "embedding_b64",
        "vector",
        "password",
        str(tmp_path).lower(),
    ):
        assert forbidden not in blob, forbidden


def test_stream_requires_a_valid_single_use_ticket(stack: Stack) -> None:
    c = stack.client
    with pytest.raises(WebSocketDisconnect), c.websocket_connect("/api/v1/analytics/stream"):
        pass
    ticket = c.post("/api/v1/analytics/stream-ticket").json()["ticket"]
    with c.websocket_connect(f"/api/v1/analytics/stream?ticket={ticket}") as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"
    with (  # a used ticket is refused
        pytest.raises(WebSocketDisconnect),
        c.websocket_connect(f"/api/v1/analytics/stream?ticket={ticket}"),
    ):
        pass
    session_ticket = c.post(f"/api/v1/sessions/{'0' * 32}/stream-ticket")
    assert (
        session_ticket.status_code == 404
    )  # and a session ticket cannot be minted for another purpose


def test_a_session_ticket_does_not_open_the_analytics_stream(stack: Stack) -> None:
    auth = stack.app.state.auth
    ticket, _ = auth.issue_ticket("a" * 32)
    with (
        pytest.raises(WebSocketDisconnect),
        stack.client.websocket_connect(f"/api/v1/analytics/stream?ticket={ticket}"),
    ):
        pass


def test_live_changes_are_announced_without_counts_and_numbers_come_from_the_database(
    env: Env,
) -> None:
    scn, worker = scene(env)
    with env.client.websocket_connect("/api/v1/analytics/stream") as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"
        scn.present(1)
        scn.recognise()
        scn.present(2)
        wait_for(lambda: len(entries(scn)) == 1)
        reasons = set()
        for _ in range(12):
            msg = json.loads(ws.receive_text())
            assert set(msg) <= {"type", "reason"}  # a notice only: never a count
            reasons.add(msg["reason"])
            if {"visitor_registered", "visit_event"} <= reasons:
                break
        assert {"visitor_registered", "visit_event"} <= reasons
    o = env.client.get("/api/v1/analytics/overview").json()
    assert (
        o["registered_visitors"],
        o["unique_visitors_in_scope"],
        o["visits"],
        o["entry_events"],
    ) == (1, 1, 1, 1)
    assert o["active_visits"] == 1 and o["stale_open_visits"] == 0  # live session: present
    worker.close()
    wait_for(lambda: env.state(scn.sid) == "ERROR")
    after = env.client.get("/api/v1/analytics/overview").json()
    assert after["active_visits"] == 0  # nobody is "present" once the session is gone
    assert after["terminal_by_basis"]["interrupted"] == 1 and after["visits"] == 1


def test_notice_reaches_the_browser_quickly_after_the_database_commit(env: Env) -> None:
    """Measured, not assumed: time from the committed visit event to the notice at a connected client
    (in-process, TestClient; the real network adds its own latency)."""
    import time

    scn, worker = scene(env)
    visits = app_of(env).state.visits
    committed: list[float] = []
    original = visits.recorder.record

    def timed(action: Any, *a: Any) -> Any:
        result = original(action, *a)
        committed.append(time.perf_counter())
        return result

    visits.recorder.record = timed
    with env.client.websocket_connect("/api/v1/analytics/stream") as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"
        scn.present(1)
        scn.recognise()
        scn.present(2)
        wait_for(lambda: len(entries(scn)) == 1)
        deadline = time.perf_counter() + 5
        arrived = None
        while time.perf_counter() < deadline:
            msg = json.loads(ws.receive_text())
            if msg["reason"] == "visit_event":
                arrived = time.perf_counter()
                break
        assert arrived is not None and committed
        latency_ms = (arrived - committed[0]) * 1000
    assert latency_ms < 500, latency_ms  # generous bound; typical values are a few ms
    print(f"analytics notice latency after commit: {latency_ms:.1f} ms")
    worker.close()
