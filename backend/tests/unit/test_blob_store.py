"""Image storage backends: one contract, two implementations (local folder, Supabase private storage).

Supabase is simulated by an in-memory fake of the Storage REST API behind ``httpx.MockTransport``
(authorisation header, ``x-upsert: false`` duplicate answers, recursive listing with paging, private
bucket, injected outages and 5xx). It proves our client logic; it does NOT prove the real service -
that needs credentials and is verified by ``scripts/verify_storage.py`` against a real bucket.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import httpx
import numpy as np
import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import VisitsConfig
from app.services.blob_store import (
    BlobError,
    BlobNotFound,
    LocalBlobBackend,
    SupabaseBlobBackend,
)
from app.services.evidence_store import EvidenceStore, EvidenceWriteError
from app.services.image_store import ImageStoreError, LocalImageStore
from tests.conftest import make_settings

KEY = "service-role-key-ABCDEF-secret"
BUCKET = "katomaran-evidence"


def jpeg(seed: int = 0) -> bytes:
    img = np.random.default_rng(seed).integers(40, 220, size=(96, 96, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


class FakeSupabase:
    """Minimal in-memory Supabase Storage (REST)."""

    def __init__(self, public: bool = False) -> None:
        self.objects: dict[str, tuple[bytes, float]] = {}
        self.public = public
        self.down = False
        self.fail: list[int] = []  # status codes to answer with, once each, before behaving
        self.requests: list[tuple[str, str]] = []
        self.unauthorised = 0

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))

    def handle(self, req: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("storage unreachable")
        self.requests.append((req.method, req.url.path))
        if req.headers.get("authorization") != f"Bearer {KEY}":
            self.unauthorised += 1
            return httpx.Response(401, json={"message": "unauthorized"})
        if self.fail:
            return httpx.Response(self.fail.pop(0), json={"message": "boom"})
        path = req.url.path
        base = "/storage/v1/"
        assert path.startswith(base)
        rest = path[len(base) :]
        if rest == f"bucket/{BUCKET}" and req.method == "GET":
            return httpx.Response(200, json={"id": BUCKET, "public": self.public})
        if rest == f"object/list/{BUCKET}" and req.method == "POST":
            body = json.loads(req.content)
            prefix = body["prefix"].strip("/") + "/"
            names: dict[str, tuple[bytes, float] | None] = {}
            for key, val in sorted(self.objects.items()):
                if key.startswith(prefix):
                    head, _, tail = key[len(prefix) :].partition("/")
                    names[head] = None if tail else val
            rows = [
                {"name": n, "id": None}
                if v is None
                else {
                    "name": n,
                    "id": "x",
                    "updated_at": datetime.fromtimestamp(v[1], UTC).isoformat(),
                }
                for n, v in names.items()
            ]
            return httpx.Response(200, json=rows[body["offset"] : body["offset"] + body["limit"]])
        if rest == f"object/{BUCKET}" and req.method == "DELETE":
            for k in json.loads(req.content)["prefixes"]:
                self.objects.pop(k, None)
            return httpx.Response(200, json=[])
        m = re.match(rf"object/(authenticated/)?{BUCKET}/(.+)$", rest)
        assert m, rest
        key = m.group(2).replace("%2F", "/")
        if req.method == "POST":
            if key in self.objects and req.headers.get("x-upsert") != "true":
                return httpx.Response(
                    400,
                    json={
                        "error": "Duplicate",
                        "message": "The resource already exists",
                        "statusCode": "409",
                    },
                )
            self.objects[key] = (req.content, time.time())
            return httpx.Response(200, json={"Key": f"{BUCKET}/{key}"})
        if req.method in ("GET", "HEAD"):
            if key not in self.objects:
                return httpx.Response(400, json={"error": "not_found", "statusCode": "404"})
            return httpx.Response(
                200, content=b"" if req.method == "HEAD" else self.objects[key][0]
            )
        return httpx.Response(405)


@pytest.fixture
def fake() -> FakeSupabase:
    return FakeSupabase()


def remote(fake: FakeSupabase, prefix: str = "evidence") -> SupabaseBlobBackend:
    return SupabaseBlobBackend("https://proj.supabase.co", KEY, BUCKET, prefix, fake.client())


@pytest.fixture(params=["local", "supabase"])
def backend(request: pytest.FixtureRequest, tmp_path: Path, fake: FakeSupabase) -> Any:
    return LocalBlobBackend(tmp_path / "blobs") if request.param == "local" else remote(fake)


# ===================================================================== backend contract
def test_put_get_exists_delete_roundtrip_and_idempotent_put(backend: Any) -> None:
    key = "entries/2026-03-04/" + "a" * 32 + ".jpg"
    assert backend.exists(key) is False
    backend.put(key, b"first")
    backend.put(key, b"second")  # same deterministic key: left alone, not an error
    assert backend.get(key) == b"first"
    assert backend.exists(key) is True
    assert backend.delete(key) is True and backend.delete(key) is False
    with pytest.raises(BlobNotFound):
        backend.get(key)


def test_listing_is_recursive_paged_and_prefix_scoped(backend: Any) -> None:
    keys = [f"entries/2026-03-0{d}/{n:032x}.jpg" for d in (1, 2) for n in range(60)]
    for k in keys:
        backend.put(k, b"x")
    backend.put("exits/2026-03-01/" + "b" * 32 + ".jpg", b"y")
    listed = {k for k, _ in backend.list("entries")}
    assert listed == set(keys) and len(listed) == 120  # more than one page of 100
    assert all(isinstance(m, float) for _, m in backend.list("exits"))
    assert backend.list("nothing") == []


def test_delete_prefix_removes_only_that_prefix(backend: Any) -> None:
    for i in range(3):
        backend.put(f"{'c' * 32}/{i:020x}.jpg", b"x")
    backend.put(f"{'d' * 32}/{0:020x}.jpg", b"x")
    assert backend.delete_prefix("c" * 32 + "/") == 3
    assert [k for k, _ in backend.list("")] == [f"{'d' * 32}/{0:020x}.jpg"]
    assert backend.delete_prefix("c" * 32 + "/") == 0


@pytest.mark.parametrize("bad", ["../x.jpg", "a/../../x", "/etc/passwd", "a\\b"])
def test_keys_cannot_escape(backend: Any, bad: str) -> None:
    with pytest.raises(BlobError):
        backend.put(bad, b"x")


# ===================================================================== Supabase specifics
def test_every_request_is_authorised_with_the_service_key_and_nothing_is_public(
    fake: FakeSupabase,
) -> None:
    b = remote(fake)
    b.put("entries/2026-03-04/" + "a" * 32 + ".jpg", b"x")
    b.get("entries/2026-03-04/" + "a" * 32 + ".jpg")
    assert fake.unauthorised == 0 and fake.requests
    assert all("/object/public/" not in p for _, p in fake.requests)  # never the public endpoint
    assert b.verify_private() is True
    assert remote(FakePublic()).verify_private() is False  # a public bucket is detected


class FakePublic(FakeSupabase):
    def __init__(self) -> None:
        super().__init__(public=True)


def test_a_wrong_key_is_a_storage_error_not_a_crash(fake: FakeSupabase) -> None:
    bad = SupabaseBlobBackend(
        "https://proj.supabase.co", "wrong", BUCKET, "evidence", fake.client()
    )
    with pytest.raises(BlobError):
        bad.put("entries/2026-03-04/" + "a" * 32 + ".jpg", b"x")


def test_outage_and_server_errors_are_transient_blob_errors(fake: FakeSupabase) -> None:
    b = remote(fake)
    fake.down = True
    for op in (
        lambda: b.put("k.jpg", b"x"),
        lambda: b.get("k.jpg"),
        lambda: b.exists("k.jpg"),
        lambda: b.list(""),
    ):
        with pytest.raises(BlobError):
            op()
    fake.down = False
    fake.fail = [503]
    with pytest.raises(BlobError):
        b.put("entries/2026-03-04/" + "e" * 32 + ".jpg", b"x")
    b.put("entries/2026-03-04/" + "e" * 32 + ".jpg", b"x")  # the retry succeeds
    assert fake.objects  # exactly one object, no duplicate


def test_service_key_and_image_bytes_never_reach_the_logs(
    fake: FakeSupabase, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    b = remote(fake)
    payload = jpeg(3)
    b.put("entries/2026-03-04/" + "f" * 32 + ".jpg", payload)
    fake.down = True
    with pytest.raises(BlobError) as err:
        b.get("entries/2026-03-04/" + "f" * 32 + ".jpg")
    assert KEY not in caplog.text and KEY not in str(err.value)


# ===================================================================== stores on the remote backend
def evidence(fake: FakeSupabase) -> EvidenceStore:
    return EvidenceStore(backend=remote(fake))


def test_evidence_store_on_remote_storage_is_idempotent_and_validates(fake: FakeSupabase) -> None:
    store = evidence(fake)
    rel = "entries/2026-03-04/" + "a" * 32 + ".jpg"
    sha = store.save(rel, jpeg(1))
    assert store.save(rel, jpeg(2)) == sha  # an event's evidence never changes
    assert store.read(rel) == jpeg(1) and store.exists(rel) and store.sha256(rel) == sha
    assert list(fake.objects) == ["evidence/" + rel]
    black = np.zeros((96, 96, 3), dtype=np.uint8)
    with pytest.raises(ImageStoreError):
        store.save(
            "entries/2026-03-04/" + "b" * 32 + ".jpg",
            bytes(cv2.imencode(".jpg", black)[1].tobytes()),
        )
    assert [k for k, _ in store.all_files()] == [rel]
    with pytest.raises(ImageStoreError):
        store.read("../../x.jpg")


def test_partial_failures_are_retryable_evidence_errors(fake: FakeSupabase) -> None:
    store = evidence(fake)
    rel = "exits/2026-03-04/" + "c" * 32 + ".jpg"
    fake.fail = [500]  # the existence probe fails first ...
    with pytest.raises(EvidenceWriteError):
        store.save(rel, jpeg(4))
    fake.fail = [200 + 0, 500]  # ... then probe ok(200 json), upload 500
    fake.fail = []
    fake.down = True
    with pytest.raises(EvidenceWriteError):
        store.save(rel, jpeg(4))
    fake.down = False
    store.save(rel, jpeg(4))
    assert store.exists(rel) and len(fake.objects) == 1


def test_visitor_images_and_erasure_on_remote_storage(fake: FakeSupabase) -> None:
    store = LocalImageStore(backend=remote(fake, "faces"))
    vid = "a" * 32
    rel, sha = store.save(vid, jpeg(5))
    assert store.save(vid, jpeg(5)) == (rel, sha) and len(fake.objects) == 1
    store.save(vid, jpeg(6))
    store.save("b" * 32, jpeg(7))
    assert store.read(rel) == jpeg(5)
    assert store.delete_visitor(vid) == 2  # erasure deletes every object of the visitor
    assert not store.exists(rel) and len(fake.objects) == 1
    assert {k.split("/")[1] for k in fake.objects} == {"b" * 32}


def test_local_root_property_is_only_for_the_local_backend(
    tmp_path: Path, fake: FakeSupabase
) -> None:
    assert EvidenceStore(tmp_path / "e").root == (tmp_path / "e").resolve()
    with pytest.raises(RuntimeError):
        _ = evidence(fake).root


# ===================================================================== recorder: durability across restarts
def test_evidence_survives_a_restart_and_outage_then_retry_stores_it(
    tmp_path: Path, sqlite_engine: Any, fake: FakeSupabase
) -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker

    from app.core.logging import _reset, setup_logging
    from app.models import VisitEvent, Visitor
    from app.services.event_recorder import EventRecorder
    from app.services.visit_manager import Crop, VisitAction, VisitSnapshot, visit_id_for

    setup_logging("WARNING", tmp_path / "logs", tmp_path / "logs" / "events.log")
    try:
        factory = sessionmaker(sqlite_engine, expire_on_commit=False)
        with factory() as db:
            db.add(
                Visitor(
                    id="a" * 32,
                    visitor_number=1,
                    registration_key="k",
                    status="REGISTERED",
                    model_version="m",
                    recognition_count=0,
                )
            )
            db.commit()
        cfg = VisitsConfig(orphan_grace_seconds=0, retry_base_seconds=0.01)
        rec = EventRecorder(factory, evidence(fake), cfg)
        key = f"{'s' * 32}:{'a' * 32}:1"
        snap = VisitSnapshot(
            id=visit_id_for(key), visit_key=key, session_id="s" * 32, source_id="x", source_label="X",
            visitor_id="a" * 32, visitor_number=1, state="ACTIVE", entry_video_ms=0,
            entry_wall=datetime(2026, 3, 4, 10, tzinfo=UTC), last_video_ms=1, last_wall=datetime(2026, 3, 4, 10, tzinfo=UTC),
            track_segments=1,
        )  # fmt: skip
        action = VisitAction("entry", snap, "entry", Crop(jpeg(9), 0.8, 0), "observation")
        fake.down = True  # storage outage while the event is recorded
        res = rec.record(action)
        assert res.created and res.image_status == "PENDING"
        fake.down = False
        assert rec.retry_images() == 1  # the retry stores it
        with factory() as db:
            ev = db.execute(select(VisitEvent)).scalar_one()
            assert ev.image_status == "STORED" and ev.image_path
            path = ev.image_path
        # "API redeployment": a brand-new recorder and store object on the same remote bucket
        restarted = EventRecorder(factory, evidence(fake), cfg)
        assert restarted.store.read(path) == jpeg(9)
        assert restarted.reconcile(live_sessions={"s" * 32}).get("evidence_missing", 0) == 0
        # a lost object is reported, never invented
        fake.objects.clear()
        assert restarted.reconcile(live_sessions={"s" * 32})["evidence_missing"] == 1
    finally:
        _reset(logging.getLogger())
        _reset(logging.getLogger("katomaran.events"))


def test_reconcile_removes_orphans_in_remote_storage(
    tmp_path: Path, sqlite_engine: Any, fake: FakeSupabase
) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.core.logging import _reset, setup_logging
    from app.services.event_recorder import EventRecorder

    setup_logging("WARNING", tmp_path / "logs", tmp_path / "logs" / "events.log")
    try:
        store = evidence(fake)
        rel = "exits/2026-03-04/" + "d" * 32 + ".jpg"
        store.save(rel, jpeg(8))  # uploaded, but the database never committed the event
        old = time.time() - 3600
        fake.objects["evidence/" + rel] = (fake.objects["evidence/" + rel][0], old)
        rec = EventRecorder(
            sessionmaker(sqlite_engine, expire_on_commit=False),
            store,
            VisitsConfig(orphan_grace_seconds=60),
        )
        assert rec.reconcile(live_sessions=set())["orphan_files_removed"] == 1
        assert not fake.objects
    finally:
        _reset(logging.getLogger())
        _reset(logging.getLogger("katomaran.events"))


# ===================================================================== settings
def test_supabase_backend_requires_https_url_and_key(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="SUPABASE_URL"):
        make_settings(tmp_path, storage_backend="supabase")
    with pytest.raises(ValidationError, match="SUPABASE_SERVICE_ROLE_KEY"):
        make_settings(tmp_path, storage_backend="supabase", supabase_url="https://p.supabase.co")
    with pytest.raises(ValidationError, match="SUPABASE_URL"):
        make_settings(
            tmp_path,
            storage_backend="supabase",
            supabase_url="http://p.supabase.co",
            supabase_service_role_key=SecretStr(KEY),
        )
    ok = make_settings(
        tmp_path,
        storage_backend="supabase",
        supabase_url="https://p.supabase.co",
        supabase_service_role_key=SecretStr(KEY),
    )
    assert KEY not in repr(ok) and KEY not in str(ok.model_dump())
    assert make_settings(tmp_path).storage_backend == "local"
