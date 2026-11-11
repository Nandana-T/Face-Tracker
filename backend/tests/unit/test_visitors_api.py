"""Visitor gallery + recognition REST API: auth, paging, search, images, erasure, no embeddings."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.main import create_app
from app.models import FaceEmbedding, IdentityAssociation, ProcessingSession, Visitor
from tests.conftest import make_full_config
from tests.unit.test_security import PASSWORD, settings_with_auth
from tests.unit.test_sources import write_video


def jpeg(color: int = 120) -> bytes:
    img = np.full((96, 96, 3), color, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


class Stack:
    def __init__(self, client: TestClient, app: Any) -> None:
        self.client = client
        self.app = app
        self.identity = app.state.identity
        self.factory = app.state.session_factory

    def seed(self, n: int, with_image: bool = True) -> list[str]:
        """n visitors, numbered 1..n, first seen one hour apart (visitor 1 the oldest)."""
        ids: list[str] = []
        base = datetime(2026, 1, 1, tzinfo=UTC)
        with self.factory() as db:
            db.add(
                ProcessingSession(
                    id="s" * 32,
                    source_id="x",
                    source_label="Hall camera",
                    source_type="file",
                    state="COMPLETED",
                    frames_read=0,
                    frames_processed=0,
                    frames_skipped=0,
                    frames_dropped=0,
                    corrupt_frames=0,
                    detections_total=0,
                    max_faces_in_frame=0,
                )
            )
            for k in range(1, n + 1):
                vid = uuid.uuid4().hex
                path = sha = None
                if with_image:
                    path, sha = self.identity._store.save(vid, jpeg(80 + k))  # noqa: SLF001
                db.add(
                    Visitor(
                        id=vid,
                        visitor_number=k,
                        registration_key=f"k{k}",
                        status="REGISTERED",
                        model_version="sface-2021dec",
                        first_session_id="s" * 32,
                        first_seen_at=base + timedelta(hours=k),
                        last_seen_at=base + timedelta(hours=100 - k),
                        recognition_count=k,
                        image_path=path,
                        image_sha256=sha,
                        image_quality=0.7,
                    )
                )
                blob = np.ones(128, dtype=np.float32).tobytes()
                db.add(
                    FaceEmbedding(
                        id=uuid.uuid4().hex,
                        visitor_id=vid,
                        model_version="sface-2021dec",
                        dim=128,
                        vector=blob,
                        vector_sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
                        quality_score=0.8,
                    )
                )
                db.add(
                    IdentityAssociation(
                        id=uuid.uuid4().hex,
                        session_id="s" * 32,
                        track_id=k,
                        visitor_id=vid,
                        state="REGISTERED",
                        best_similarity=None,
                        observations=5,
                        conflicts=0,
                        registered_here=1,
                    )
                )
                ids.append(vid)
            db.commit()
        return ids

    def get(self, path: str, **kw: Any) -> Any:
        return self.client.get(f"/api/v1{path}", **kw)


@pytest.fixture
def stack(tmp_path: Path, sqlite_engine: Engine) -> Any:
    video = write_video(tmp_path / "clip.mp4")
    cfg = make_full_config(
        tmp_path,
        video={
            "sample_dir": str(tmp_path / "none"),
            "sources": [{"id": "clip", "label": "Clip", "type": "file", "path": str(video)}],
        },
    )
    app = create_app(settings=settings_with_auth(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        token = client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()[
            "access_token"
        ]
        client.headers["Authorization"] = f"Bearer {token}"
        yield Stack(client, app)


ROUTES = [
    ("GET", "/api/v1/visitors"),
    ("GET", "/api/v1/visitors/" + "0" * 32),
    ("GET", "/api/v1/visitors/" + "0" * 32 + "/image"),
    ("DELETE", "/api/v1/visitors/" + "0" * 32),
    ("GET", "/api/v1/recognition/status"),
    ("GET", "/api/v1/recognition/stats"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_visitor_route_requires_a_token(stack: Stack, method: str, path: str) -> None:
    r = TestClient(stack.app).request(method, path)  # a client without the bearer header
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


def test_empty_gallery_is_an_empty_list_not_fake_data(stack: Stack) -> None:
    body = stack.get("/visitors").json()
    assert body == {"items": [], "total": 0, "limit": 24, "offset": 0}


def test_list_paginates_and_sorts(stack: Stack) -> None:
    stack.seed(7)
    page1 = stack.get("/visitors?limit=3&sort=number&order=asc").json()
    assert page1["total"] == 7 and [v["number"] for v in page1["items"]] == [1, 2, 3]
    page3 = stack.get("/visitors?limit=3&offset=6&sort=number&order=asc").json()
    assert [v["number"] for v in page3["items"]] == [7]
    first_seen = stack.get("/visitors?sort=first_seen&order=desc&limit=2").json()["items"]
    assert [v["number"] for v in first_seen] == [7, 6]
    last_seen = stack.get("/visitors?sort=last_seen&order=desc&limit=2").json()["items"]
    assert [v["number"] for v in last_seen] == [1, 2]  # visitor 1 was recognised most recently
    assert stack.get("/visitors?limit=0").status_code == 422
    assert stack.get("/visitors?limit=101").status_code == 422
    assert stack.get("/visitors?sort=embedding").status_code == 422


def test_search_by_label_number_or_id_prefix(stack: Stack) -> None:
    ids = stack.seed(12)
    by_label = stack.get("/visitors?q=V00005").json()
    assert [v["number"] for v in by_label["items"]] == [5]
    assert [v["number"] for v in stack.get("/visitors?q=12").json()["items"]] == [12]
    by_id = stack.get(f"/visitors?q={ids[2][:8]}").json()
    assert [v["number"] for v in by_id["items"]] == [3]
    assert stack.get("/visitors?q=nobody").json()["total"] == 0
    assert stack.get("/visitors?q=%25%27%3B--").json()["total"] == 0  # hostile input is just text


def test_responses_never_contain_embeddings_or_paths(stack: Stack) -> None:
    ids = stack.seed(2)
    texts = [
        stack.get("/visitors").text,
        stack.get(f"/visitors/{ids[0]}").text,
        stack.get("/recognition/stats").text,
        stack.get("/recognition/status").text,
        stack.get("/config").text,
    ]
    for t in texts:
        low = t.lower()
        for forbidden in ("vector", "image_path", ".jpg", "storage", "sha256", "faces_dir"):
            assert forbidden not in low, forbidden
    item = json.loads(texts[0])["items"][0]
    assert set(item) == {
        "id",
        "number",
        "label",
        "status",
        "first_seen_at",
        "last_seen_at",
        "recognition_count",
        "has_image",
        "model_version",
    }


def test_detail_shows_recognition_history_with_session_scoped_track_ids(stack: Stack) -> None:
    ids = stack.seed(3)
    d = stack.get(f"/visitors/{ids[1]}").json()
    assert d["number"] == 2 and d["label"] == "V00002" and d["has_image"] is True
    assert d["sessions_count"] == 1 and d["tracks_count"] == 1
    a = d["associations"][0]
    assert a["session_id"] == "s" * 32 and a["track_id"] == 2 and a["source_label"] == "Hall camera"
    assert a["registered_in_session"] is True and a["state"] == "REGISTERED"
    assert stack.get("/visitors/V00002").json()["id"] == ids[1]  # the short label works too


def test_unknown_or_malformed_ids_are_404_not_500(stack: Stack) -> None:
    stack.seed(1)
    for bad in (
        "0" * 32,
        "V99999",
        "../../etc/passwd",
        "%2e%2e%2f",
        "x" * 40,
        "'; drop table visitors;--",
    ):
        r = stack.get(f"/visitors/{bad}")
        assert r.status_code in (404, 405), bad
    assert stack.get("/visitors/" + "0" * 32 + "/image").status_code == 404


def test_image_is_served_only_through_the_authenticated_endpoint(
    stack: Stack, tmp_path: Path
) -> None:
    ids = stack.seed(1)
    r = stack.get(f"/visitors/{ids[0]}/image")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, no-store" and r.content.startswith(b"\xff\xd8")
    assert TestClient(stack.app).get(f"/api/v1/visitors/{ids[0]}/image").status_code == 401
    # there is no static route to the face directory
    for path in ("/faces", "/storage", f"/faces/{ids[0]}", "/api/v1/faces", "/static/faces"):
        assert stack.client.get(path).status_code in (401, 404)


def test_missing_image_and_tampered_reference_are_safe_404s(stack: Stack) -> None:
    ids = stack.seed(2)
    store = stack.identity._store  # noqa: SLF001
    with stack.factory() as db:
        db.get(Visitor, ids[0]).image_path = None
        db.get(Visitor, ids[1]).image_path = "../../../../Windows/win.ini"
        db.commit()
    assert stack.get(f"/visitors/{ids[0]}/image").status_code == 404
    assert (
        stack.get(f"/visitors/{ids[1]}/image").status_code == 404
    )  # traversal attempt never reads a file
    assert store.all_files() != []


def test_erasure_removes_everything_and_is_not_repeatable(stack: Stack) -> None:
    ids = stack.seed(2)
    assert stack.client.delete(f"/api/v1/visitors/{ids[0]}").status_code == 204
    assert stack.client.delete(f"/api/v1/visitors/{ids[0]}").status_code == 404
    assert stack.get(f"/visitors/{ids[0]}").status_code == 404
    assert stack.get(f"/visitors/{ids[0]}/image").status_code == 404
    assert stack.get("/visitors").json()["total"] == 1
    assert len(stack.identity._store.all_files()) == 1  # noqa: SLF001 - the other visitor's image stays


def test_recognition_status_and_stats(stack: Stack) -> None:
    stack.seed(3)
    stack.identity.load()
    status = stack.get("/recognition/status").json()
    assert status["enabled"] is True and status["registered_visitors"] == 3
    assert status["worker_available"] is None  # no worker connected in this test
    stats = stack.get("/recognition/stats").json()
    assert stats["registered_visitors"] == 3 and stats["associations"] == 3
    assert stats["thresholds"]["similarity_threshold"] > stats["thresholds"]["novelty_threshold"]
    assert stats["runtime"]["model_version"] == "sface-2021dec"


def test_recognition_disabled_is_reported_not_crashed(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    cfg = make_full_config(tmp_path, recognition={"enabled": False})
    app = create_app(settings=settings_with_auth(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        token = client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()[
            "access_token"
        ]
        client.headers["Authorization"] = f"Bearer {token}"
        assert client.get("/api/v1/visitors").status_code == 503
        assert client.get("/api/v1/visitors").json()["error"]["code"] == "recognition_disabled"
        status = client.get("/api/v1/recognition/status").json()
        assert status["enabled"] is False and status["registered_visitors"] == 0


def test_public_config_hides_paths_of_the_recognition_section(stack: Stack) -> None:
    rec = stack.get("/config").json()["recognition"]
    assert "faces_dir" not in rec and "model_path" not in rec
    assert rec["similarity_threshold"] == 0.5 and rec["novelty_threshold"] == 0.4
