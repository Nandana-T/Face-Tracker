# ruff: noqa: F811, F401
"""The whole API on top of REMOTE (Supabase-style) private storage, against an in-memory fake.

Covers Gate B checks that need the application: authenticated retrieval only, restart persistence, erasure
removing visitor images AND evidence, missing objects, and no credential/object-path leakage to the browser.
The fake proves our client logic; the real service is verified separately by scripts/verify_storage.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

import app.main as main_module
from app.main import create_app
from app.services.blob_store import SupabaseBlobBackend
from app.services.evidence_store import EvidenceStore
from app.services.image_store import LocalImageStore
from tests.conftest import make_full_config
from tests.unit.test_blob_store import KEY, FakeSupabase, jpeg
from tests.unit.test_events_api import BASE, seed_visits
from tests.unit.test_security import PASSWORD, settings_with_auth
from tests.unit.test_visitors_api import Stack


def build(
    tmp_path: Path, engine: Engine, fake: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> Any:
    def remote_stores(settings: Any, config: Any, client: Any = None) -> Any:
        faces = SupabaseBlobBackend(
            "https://proj.supabase.co", KEY, "katomaran-evidence", "faces", fake.client()
        )
        evidence = SupabaseBlobBackend(
            "https://proj.supabase.co", KEY, "katomaran-evidence", "evidence", fake.client()
        )
        return LocalImageStore(backend=faces), EvidenceStore(backend=evidence), "supabase"

    monkeypatch.setattr(main_module, "build_stores", remote_stores)
    return create_app(
        settings=settings_with_auth(tmp_path), config=make_full_config(tmp_path), engine=engine
    )


def login(client: TestClient) -> None:
    token = client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"


@pytest.fixture
def fake() -> FakeSupabase:
    return FakeSupabase()


@pytest.fixture
def remote_stack(
    tmp_path: Path, sqlite_engine: Engine, fake: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> Any:
    app = build(tmp_path, sqlite_engine, fake, monkeypatch)
    with TestClient(app) as client:
        login(client)
        yield Stack(client, app)


def seed_with_visitor_images(st: Stack, n: int = 2) -> list[str]:
    ids = seed_visits(st, n)  # visits + events + evidence (written through the remote backend)
    store: LocalImageStore = st.identity._store  # noqa: SLF001
    with st.factory() as db:
        from sqlalchemy import select

        from app.models import Visitor

        for v in db.execute(select(Visitor)).scalars():
            path, sha = store.save(v.id, jpeg(int(v.visitor_number) + 20))
            v.image_path, v.image_sha256 = path, sha
        db.commit()
    return ids


def test_images_live_in_the_private_bucket_under_stable_prefixes(
    remote_stack: Stack, fake: FakeSupabase
) -> None:
    seed_with_visitor_images(remote_stack, 2)
    keys = sorted(fake.objects)
    assert len([k for k in keys if k.startswith("faces/")]) == 2
    assert (
        len([k for k in keys if k.startswith("evidence/entries/")]) == 2
        and len([k for k in keys if k.startswith("evidence/exits/")]) == 2
    )
    assert fake.unauthorised == 0 and all("/object/public/" not in p for _, p in fake.requests)
    assert all(m in ("GET", "POST", "HEAD", "DELETE") for m, _ in fake.requests)


def test_retrieval_goes_only_through_authenticated_endpoints(
    remote_stack: Stack, fake: FakeSupabase
) -> None:
    seed_with_visitor_images(remote_stack, 1)
    visitor = remote_stack.get("/visitors").json()["items"][0]
    event = remote_stack.get("/events?type=entry").json()["items"][0]
    anonymous = TestClient(remote_stack.app)
    for path in (f"/api/v1/visitors/{visitor['id']}/image", f"/api/v1/events/{event['id']}/image"):
        assert anonymous.get(path).status_code == 401
        ok = remote_stack.client.get(path)
        assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
        assert ok.headers["cache-control"] == "private, no-store" and ok.content.startswith(
            b"\xff\xd8"
        )


def test_no_credentials_or_object_paths_reach_the_browser(
    remote_stack: Stack, fake: FakeSupabase
) -> None:
    seed_with_visitor_images(remote_stack, 2)
    paths = [
        "/visitors",
        "/events",
        "/visits",
        "/analytics/overview",
        "/analytics/recent",
        "/analytics/coverage",
        "/events/health",
        "/system/status",
        "/config",
    ]
    blob = json.dumps([remote_stack.get(p).json() for p in paths]).lower()
    for forbidden in (
        KEY.lower(),
        "supabase",
        "service",
        "faces/",
        "evidence/",
        "entries/",
        "exits/",
        ".jpg",
        "katomaran-evidence",
        "storage/v1",
    ):
        assert forbidden not in blob, forbidden


def test_images_persist_across_an_api_restart(
    tmp_path: Path, sqlite_engine: Engine, fake: FakeSupabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    app1 = build(tmp_path, sqlite_engine, fake, monkeypatch)
    with TestClient(app1) as c1:
        login(c1)
        seed_with_visitor_images(Stack(c1, app1), 1)
        event_id = c1.get("/api/v1/events?type=entry").json()["items"][0]["id"]
        before = c1.get(f"/api/v1/events/{event_id}/image").content
    # a brand-new application object (redeploy): same database, same bucket, nothing local
    app2 = build(tmp_path, sqlite_engine, fake, monkeypatch)
    with TestClient(app2) as c2:
        login(c2)
        assert c2.get(f"/api/v1/events/{event_id}/image").content == before
        assert c2.get("/api/v1/events/health").json()["events_by_image_status"] == {"STORED": 2}
        assert c2.post("/api/v1/events/reconcile").json()["result"].get("evidence_missing", 0) == 0
    assert (
        not list((tmp_path / "evidence").glob("**/*.jpg"))
        if (tmp_path / "evidence").exists()
        else True
    )  # nothing was written locally


def test_erasure_removes_the_visitors_images_and_evidence_from_the_bucket(
    remote_stack: Stack, fake: FakeSupabase
) -> None:
    seed_with_visitor_images(remote_stack, 2)
    first = remote_stack.get("/visitors?sort=number&order=asc").json()["items"][0]
    assert len(fake.objects) == 6  # 2 visitor images + 2 entries + 2 exits
    assert remote_stack.client.delete(f"/api/v1/visitors/{first['id']}").status_code == 204
    left = sorted(fake.objects)
    assert len(left) == 3 and not any(first["id"] in k for k in left)  # visitor image gone
    assert (
        len([k for k in left if k.startswith("evidence/")]) == 2
    )  # only the other visitor's evidence remains
    events = remote_stack.get("/events?visitor=V00001").json()
    assert (
        events["total"] == 0
    )  # the anonymised audit rows are no longer attributed to the erased visitor
    erased = [e for e in remote_stack.get("/events").json()["items"] if e["visitor"] is None]
    assert len(erased) == 2 and all(e["image_status"] == "ERASED" for e in erased)


def test_a_missing_object_is_a_404_and_is_flagged_by_reconciliation(
    remote_stack: Stack, fake: FakeSupabase
) -> None:
    seed_with_visitor_images(remote_stack, 1)
    event = remote_stack.get("/events?type=entry").json()["items"][0]
    for k in [k for k in fake.objects if k.startswith("evidence/entries/")]:
        del fake.objects[k]  # lost in the bucket
    assert remote_stack.get(f"/events/{event['id']}/image").status_code == 404
    assert (
        remote_stack.client.post("/api/v1/events/reconcile").json()["result"]["evidence_missing"]
        == 1
    )
    assert remote_stack.get(f"/events/{event['id']}").json()["image_status"] == "MISSING"


def test_a_storage_outage_degrades_to_pending_evidence_not_an_error(
    remote_stack: Stack, fake: FakeSupabase
) -> None:
    seed_with_visitor_images(remote_stack, 1)
    visitor = remote_stack.get("/visitors").json()["items"][0]
    fake.down = True
    r = remote_stack.get(f"/visitors/{visitor['id']}/image")
    assert r.status_code == 404 and KEY not in r.text  # a clean error, never a stack trace or a key
    fake.down = False
    assert remote_stack.get(f"/visitors/{visitor['id']}/image").status_code == 200
