# ruff: noqa: F811, F401
"""Event / visit REST API: auth, paging, filters, privacy (no paths/embeddings), images, erasure, health."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.main import create_app
from app.services.visit_manager import Crop, VisitAction, VisitSnapshot, visit_id_for
from tests.conftest import make_full_config
from tests.unit.test_security import PASSWORD, settings_with_auth
from tests.unit.test_sources import write_video
from tests.unit.test_visitors_api import Stack, stack

BASE = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
SID = "s" * 32


def pic(seed: int) -> bytes:
    img = np.random.default_rng(seed).integers(30, 230, size=(96, 96, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


def seed_visits(
    st: Stack, n: int = 4, interrupted: tuple[int, ...] = (), open_: tuple[int, ...] = ()
) -> list[str]:
    """n visitors (V00001..), visit k enters at +k hours and leaves 10 minutes later."""
    ids = st.seed(n, with_image=False)
    recorder = st.app.state.visits.recorder
    visit_ids: list[str] = []
    for k, visitor in enumerate(ids, start=1):
        key = f"{SID}:{visitor}:1"
        vid = visit_id_for(key)
        visit_ids.append(vid)
        entry_at = BASE + timedelta(hours=k)

        def snap(
            *,
            _vid: str = vid,
            _key: str = key,
            _visitor: str = visitor,
            _k: int = k,
            _at: datetime = entry_at,
            **kw: Any,
        ) -> VisitSnapshot:
            base: dict[str, Any] = dict(
                id=_vid, visit_key=_key, session_id=SID, source_id="x", source_label="Hall camera",
                visitor_id=_visitor, visitor_number=_k, state="ACTIVE", entry_video_ms=1000,
                entry_wall=_at, last_video_ms=601_000, last_wall=_at + timedelta(minutes=10),
                track_segments=1,
            )  # fmt: skip
            return VisitSnapshot(**{**base, **kw})

        recorder.record(
            VisitAction("entry", snap(), "entry", Crop(pic(k), 0.8, 1000), "observation")
        )
        if k in open_:
            continue
        if k in interrupted:
            terminal = snap(
                state="EXIT_PENDING", exit_basis="interrupted", closure_reason="worker_lost"
            )
            recorder.record(
                VisitAction(
                    "terminal",
                    terminal,
                    "interrupted",
                    Crop(pic(50 + k), 0.7, 5),
                    "last_good_observation",
                )
            )
        else:
            terminal = snap(
                state="EXIT_PENDING", exit_basis="absence", closure_reason="absence_timeout"
            )
            recorder.record(
                VisitAction(
                    "terminal", terminal, "exit", Crop(pic(50 + k), 0.7, 5), "last_good_observation"
                )
            )
    return visit_ids


ROUTES = [
    ("GET", "/api/v1/events"),
    ("GET", "/api/v1/events/" + "0" * 32),
    ("GET", "/api/v1/events/" + "0" * 32 + "/image"),
    ("GET", "/api/v1/events/health"),
    ("POST", "/api/v1/events/reconcile"),
    ("GET", "/api/v1/events/log-verification"),
    ("GET", "/api/v1/visits"),
    ("GET", "/api/v1/visits/" + "0" * 32),
    ("GET", "/api/v1/visits/" + "0" * 32 + "/events"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_event_route_requires_a_token(stack: Stack, method: str, path: str) -> None:
    r = TestClient(stack.app).request(method, path)
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_empty_log_is_an_empty_list_not_fake_data(stack: Stack) -> None:
    assert stack.get("/events").json() == {"items": [], "total": 0, "limit": 25, "offset": 0}
    assert stack.get("/visits").json()["items"] == []


def test_list_paginates_sorts_and_filters(stack: Stack) -> None:
    seed_visits(stack, 4, interrupted=(3,))
    body = stack.get("/events?limit=3&sort=event_time&order=asc").json()
    assert body["total"] == 8 and len(body["items"]) == 3
    times = [e["event_time"] for e in body["items"]]
    assert times == sorted(times)
    assert body["items"][0]["type"] == "entry" and body["items"][0]["visitor"]["label"] == "V00001"
    page = stack.get("/events?limit=3&offset=6&order=asc").json()["items"]
    assert len(page) == 2
    newest = stack.get("/events?limit=1").json()["items"][0]
    assert newest["visitor"]["label"] == "V00004" and newest["type"] == "exit"
    assert stack.get("/events?type=entry").json()["total"] == 4
    assert stack.get("/events?type=exit").json()["total"] == 3
    interrupted = stack.get("/events?type=interrupted").json()
    assert interrupted["total"] == 1 and interrupted["items"][0]["visitor"]["label"] == "V00003"
    assert interrupted["items"][0]["exit_basis"] == "interrupted"
    since = (BASE + timedelta(hours=2, minutes=30)).isoformat().replace("+00:00", "Z")
    assert stack.get("/events", params={"since": since}).json()["total"] == 4  # visits 3 and 4
    until = (BASE + timedelta(hours=1, minutes=30)).isoformat().replace("+00:00", "Z")
    assert (
        stack.get("/events", params={"until": until}).json()["total"] == 2
    )  # visit 1: entry + exit
    for bad in (
        "limit=0",
        "limit=101",
        "type=teleport",
        "sort=embedding",
        "session_id=zz",
        "persistence_status=NOPE",
    ):
        assert stack.get(f"/events?{bad}").status_code == 422, bad


def test_search_by_visitor_label_number_or_id_prefix(stack: Stack) -> None:
    seed_visits(stack, 3)
    ids = {e["visitor"]["id"] for e in stack.get("/events").json()["items"]}
    assert stack.get("/events?visitor=V00002").json()["total"] == 2
    assert stack.get("/events?visitor=2").json()["total"] == 2
    prefix = next(iter(ids))[:8]
    assert stack.get(f"/events?visitor={prefix}").json()["total"] == 2
    assert stack.get("/events?visitor=zzzz").json()["total"] == 0
    assert stack.get("/events?visitor=V00099").json()["total"] == 0


def test_event_detail_and_unknown_ids(stack: Stack) -> None:
    seed_visits(stack, 1)
    item = stack.get("/events").json()["items"][0]
    detail = stack.get(f"/events/{item['id']}").json()
    assert detail == item
    assert detail["persistence_status"] == "DELIVERED" and detail["has_image"] is True
    for bad in ("0" * 32, "nothex", "../../etc"):
        assert stack.get(f"/events/{bad}").status_code == 404


def test_responses_never_contain_paths_embeddings_or_track_ids(
    stack: Stack, tmp_path: Path
) -> None:
    visit_ids = seed_visits(stack, 2)
    blob = json.dumps(
        [
            stack.get("/events").json(),
            stack.get("/visits").json(),
            stack.get(f"/visits/{visit_ids[0]}").json(),
            stack.get("/events/health").json(),
        ]
    ).lower()
    for forbidden in (
        ".jpg",
        "image_path",
        "evidence",
        "entries/",
        "exits/",
        str(tmp_path).lower(),
        "embedding",
        "vector",
        "track_id",
    ):
        assert forbidden not in blob, forbidden


def test_image_is_served_only_through_the_authenticated_endpoint(stack: Stack) -> None:
    seed_visits(stack, 1)
    entry = stack.get("/events?type=entry").json()["items"][0]
    r = stack.get(f"/events/{entry['id']}/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, no-store" and r.content.startswith(b"\xff\xd8")
    assert TestClient(stack.app).get(f"/api/v1/events/{entry['id']}/image").status_code == 401
    for path in ("/logs/entries", "/evidence", "/static/entries", "/entries"):
        assert stack.client.get(path).status_code in (401, 404)  # no static route exists
    assert stack.get(f"/events/{'1' * 32}/image").status_code == 404


def test_missing_image_is_a_404_and_visible_as_evidence_missing(stack: Stack) -> None:
    seed_visits(stack, 1)
    entry = stack.get("/events?type=entry").json()["items"][0]
    recorder = stack.app.state.visits.recorder
    for f in recorder.store.all_files():
        (recorder.store.root / f[0]).unlink()
    recorder.reconcile(live_sessions=set())
    assert stack.get(f"/events/{entry['id']}/image").status_code == 404
    again = stack.get(f"/events/{entry['id']}").json()
    assert again["image_status"] == "MISSING" and again["has_image"] is False
    visit = stack.get(f"/visits/{entry['visit_id']}").json()
    assert visit["integrity"] == "evidence_missing"


def test_visits_listing_detail_duration_and_integrity(stack: Stack) -> None:
    visit_ids = seed_visits(stack, 4, interrupted=(3,), open_=(4,))
    listing = stack.get("/visits?sort=entry_time&order=asc").json()
    assert listing["total"] == 4
    by_label = {v["visitor"]["label"]: v for v in listing["items"]}
    assert (
        by_label["V00001"]["state"] == "CLOSED" and by_label["V00001"]["duration_seconds"] == 600.0
    )
    assert by_label["V00001"]["integrity"] == "complete"
    assert (
        by_label["V00003"]["state"] == "INTERRUPTED"
        and by_label["V00003"]["duration_seconds"] is None
    )
    assert (
        by_label["V00004"]["state"] == "ACTIVE" and by_label["V00004"]["duration_seconds"] is None
    )
    assert by_label["V00004"]["exit_time"] is None and by_label["V00004"]["integrity"] == "open"
    assert stack.get("/visits?state=INTERRUPTED").json()["total"] == 1
    assert stack.get("/visits?visitor=V00002").json()["total"] == 1
    detail = stack.get(f"/visits/{visit_ids[0]}").json()
    assert [e["type"] for e in detail["events"]] == ["entry", "exit"]
    assert detail["id"] != detail["visitor"]["id"]  # visit id and visitor id are different things
    assert stack.get(f"/visits/{visit_ids[0]}/events").json() == detail["events"]
    assert stack.get("/visits/" + "0" * 32).status_code == 404
    assert stack.get("/visits/xyz").status_code == 404


def test_a_returning_visitor_has_several_visits_and_one_identity(stack: Stack) -> None:
    ids = stack.seed(1, with_image=False)
    recorder = stack.app.state.visits.recorder
    for ordinal in (1, 2):
        key = f"{SID}:{ids[0]}:{ordinal}"
        at = BASE + timedelta(hours=ordinal)
        snap = VisitSnapshot(
            id=visit_id_for(key), visit_key=key, session_id=SID, source_id="x", source_label="Hall",
            visitor_id=ids[0], visitor_number=1, state="ACTIVE", entry_video_ms=0, entry_wall=at,
            last_video_ms=9000, last_wall=at + timedelta(seconds=9), track_segments=1,
        )  # fmt: skip
        recorder.record(
            VisitAction("entry", snap, "entry", Crop(pic(ordinal), 0.8, 0), "observation")
        )
    visits = stack.get("/visits").json()["items"]
    assert len(visits) == 2 and len({v["id"] for v in visits}) == 2
    assert {v["visitor"]["id"] for v in visits} == {ids[0]}
    assert stack.get("/visitors").json()["total"] == 1  # still ONE visitor


def test_health_reconcile_and_log_verification(stack: Stack) -> None:
    seed_visits(stack, 2)
    health = stack.get("/events/health").json()
    assert health["enabled"] is True and health["events_by_persistence"] == {"DELIVERED": 4}
    assert health["events_by_image_status"] == {"STORED": 4} and health["open_visits"] == 0
    assert health["events_log_present"] is True and health["events_log_bytes"] > 0
    assert (
        set(health["latency"]) == {"db", "image", "log"} and health["latency"]["db"]["count"] == 4
    )
    rec = stack.client.post("/api/v1/events/reconcile").json()
    assert rec["result"].get("visits_interrupted", 0) == 0
    report = stack.get("/events/log-verification").json()
    assert report["database_events"] == 4 and report["logged_events"] == 4
    assert report["consistent"] is True and report["missing_in_log"] == []


def test_erasing_a_visitor_removes_their_evidence_and_anonymises_their_events(stack: Stack) -> None:
    seed_visits(stack, 2)
    visitor = next(e["visitor"]["id"] for e in stack.get("/events?visitor=V00001").json()["items"])
    events = stack.get("/events?visitor=V00001").json()["items"]
    assert len(events) == 2 and all(e["has_image"] for e in events)
    store = stack.app.state.visits.recorder.store
    assert len(store.all_files()) == 4
    assert stack.client.delete(f"/api/v1/visitors/{visitor}").status_code == 204
    assert len(store.all_files()) == 2  # only the other visitor's evidence is left
    for e in events:
        after = stack.get(f"/events/{e['id']}").json()
        assert (
            after["visitor"] is None
            and after["image_status"] == "ERASED"
            and not after["has_image"]
        )
        assert stack.get(f"/events/{e['id']}/image").status_code == 404
    assert stack.get("/events?visitor=V00001").json()["total"] == 0
    assert stack.get("/events").json()["total"] == 4  # the audit rows survive, anonymised


def test_events_api_reports_disabled_when_the_feature_is_off(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    cfg = make_full_config(tmp_path, visits={"enabled": False})
    app = create_app(settings=settings_with_auth(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        token = client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()[
            "access_token"
        ]
        client.headers["Authorization"] = f"Bearer {token}"
        assert client.get("/api/v1/events/health").json()["enabled"] is False
        r = client.get("/api/v1/events")
        assert r.status_code == 503 and r.json()["error"]["code"] == "events_disabled"


def test_an_entry_always_sorts_before_its_terminal_event_even_when_their_times_are_equal(
    stack: Stack,
) -> None:
    """An interrupted visit that was never observed again has exit time == entry time."""
    ids = stack.seed(1, with_image=False)
    key = f"{SID}:{ids[0]}:1"
    snap = VisitSnapshot(
        id=visit_id_for(key), visit_key=key, session_id=SID, source_id="x", source_label="Hall",
        visitor_id=ids[0], visitor_number=1, state="ACTIVE", entry_video_ms=0, entry_wall=BASE,
        last_video_ms=0, last_wall=BASE, track_segments=1,
    )  # fmt: skip
    recorder = stack.app.state.visits.recorder
    recorder.record(VisitAction("entry", snap, "entry", Crop(pic(1), 0.8, 0), "observation"))
    ended = VisitSnapshot(
        **{
            **snap.__dict__,
            "state": "EXIT_PENDING",
            "exit_basis": "interrupted",
            "closure_reason": "api_restarted",
        }
    )
    recorder.record(VisitAction("terminal", ended, "interrupted", None, None))
    asc = stack.get("/events?order=asc").json()["items"]
    assert [e["type"] for e in asc] == ["entry", "interrupted"]
    desc = stack.get("/events?order=desc").json()["items"]
    assert [e["type"] for e in desc] == ["interrupted", "entry"]
    detail = stack.get(f"/visits/{snap.id}").json()
    assert [e["type"] for e in detail["events"]] == ["entry", "interrupted"]
