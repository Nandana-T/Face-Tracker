# ruff: noqa: F811, F401
"""Video upload: authentication, validation, safe naming, size limit, listing, deletion."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.main import create_app
from tests.conftest import make_full_config
from tests.unit.test_security import PASSWORD, settings_with_auth
from tests.unit.test_sessions_api import FakeWorker, env, wait_for
from tests.unit.test_sources import write_video
from tests.unit.test_visitors_api import Stack, stack


def video_bytes(tmp_path: Path) -> bytes:
    return write_video(tmp_path / "up.mp4").read_bytes()


def upload(client: TestClient, name: str, body: bytes, **kw: Any) -> Any:
    return client.put("/api/v1/uploads", params={"filename": name}, content=body, **kw)


def stored(stack: Stack) -> list[Path]:
    folder = Path(stack.app.state.config.video.upload_dir)
    return sorted(folder.iterdir()) if folder.exists() else []


def test_upload_routes_need_a_token(stack: Stack) -> None:
    anon = TestClient(stack.app)
    assert (
        anon.put("/api/v1/uploads", params={"filename": "a.mp4"}, content=b"x").status_code == 401
    )
    assert anon.delete("/api/v1/uploads/" + "0" * 32).status_code == 401


def test_a_real_video_becomes_a_source_without_exposing_paths(stack: Stack, tmp_path: Path) -> None:
    r = upload(stack.client, "My Clip (1).mp4", video_bytes(tmp_path))
    assert r.status_code == 201
    src = r.json()
    assert re.fullmatch(r"upload-[0-9a-f]{32}", src["id"]) and src["origin"] == "upload"
    assert (
        src["label"] == "Upload: My Clip (1).mp4"
        and src["type"] == "file"
        and src["availability"] == "available"
    )
    assert str(tmp_path) not in r.text and "uploads" not in r.text.lower().replace("upload:", "")
    listed = {s["id"]: s for s in stack.get("/sources").json()["sources"]}
    assert listed[src["id"]]["origin"] == "upload"
    names = [p.name for p in stored(stack)]
    assert any(re.fullmatch(r"[0-9a-f]{32}\.mp4", n) for n in names) and any(
        n.endswith(".json") for n in names
    )
    assert not any("Clip" in n for n in names)  # the client's name is never used on disk
    assert not any(n.endswith(".part") for n in names)


@pytest.mark.parametrize(
    "name",
    ["../../evil.mp4", "..\\..\\evil.mp4", "C:\\Windows\\x.mp4", "a/b/c.mp4", "<script>.mp4"],
)
def test_hostile_file_names_are_sanitised_to_a_label(
    stack: Stack, tmp_path: Path, name: str
) -> None:
    r = upload(stack.client, name, video_bytes(tmp_path))
    assert r.status_code == 201
    label = r.json()["label"]
    assert "/" not in label and "\\" not in label and ".." not in label and "<" not in label
    folder = Path(stack.app.state.config.video.upload_dir).resolve()
    assert all(
        p.resolve().parent == folder for p in stored(stack)
    )  # nothing escaped the upload folder


@pytest.mark.parametrize(
    "name", ["virus.exe", "notes.txt", "noextension", "archive.zip", "x.mp4.exe"]
)
def test_only_video_extensions_are_accepted(stack: Stack, name: str) -> None:
    r = upload(stack.client, name, b"data")
    assert r.status_code == 415 and r.json()["error"]["code"] == "unsupported_type"
    assert stored(stack) == []


def test_a_file_that_is_not_really_a_video_is_refused_and_removed(stack: Stack) -> None:
    for body in (b"this is just text" * 100, b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200):
        r = upload(stack.client, "fake.mp4", body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "not_a_video"
    assert stored(stack) == []  # no leftovers, not even a .part file


def test_an_empty_upload_is_refused(stack: Stack) -> None:
    assert upload(stack.client, "empty.mp4", b"").status_code == 422
    assert stored(stack) == []


def test_the_size_limit_applies_to_declared_and_streamed_bodies(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    cfg = make_full_config(tmp_path, video={"max_upload_mb": 1})
    app = create_app(settings=settings_with_auth(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        client.headers["Authorization"] = (
            "Bearer "
            + client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()["access_token"]
        )
        big = b"\x00" * (1024 * 1024 + 10)
        declared = upload(client, "big.mp4", big)
        assert declared.status_code == 413 and declared.json()["error"]["code"] == "too_large"

        def chunks() -> Any:  # no Content-Length: the cap must still hold while streaming
            for _ in range(3):
                yield b"\x00" * (512 * 1024)

        streamed = client.put("/api/v1/uploads", params={"filename": "big.mp4"}, content=chunks())
        assert streamed.status_code == 413
        folder = tmp_path / "uploads"
        assert not folder.exists() or list(folder.iterdir()) == []  # the partial file was deleted


def test_delete_removes_the_video_and_its_label(stack: Stack, tmp_path: Path) -> None:
    sid = upload(stack.client, "gone.mp4", video_bytes(tmp_path)).json()["id"]
    uid = sid.removeprefix("upload-")
    assert stack.client.delete(f"/api/v1/uploads/{uid}").status_code == 204
    assert stored(stack) == []
    assert sid not in {s["id"] for s in stack.get("/sources").json()["sources"]}
    assert stack.client.delete(f"/api/v1/uploads/{uid}").status_code == 404


@pytest.mark.parametrize("bad", ["../x", "nothex", "0" * 31, "G" * 32, "0" * 32])
def test_delete_validates_the_id(stack: Stack, bad: str) -> None:
    assert stack.client.delete(f"/api/v1/uploads/{bad}").status_code in (404, 405, 422)


def test_uploads_can_be_switched_off(tmp_path: Path, sqlite_engine: Engine) -> None:
    cfg = make_full_config(tmp_path, video={"uploads_enabled": False})
    app = create_app(settings=settings_with_auth(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        client.headers["Authorization"] = (
            "Bearer "
            + client.post("/api/v1/auth/login", json={"password": PASSWORD}).json()["access_token"]
        )
        r = upload(client, "a.mp4", b"x")
        assert r.status_code == 404 and r.json()["error"]["code"] == "uploads_disabled"


def test_an_upload_in_use_cannot_be_deleted_until_the_session_stops(
    env: Any, tmp_path: Path
) -> None:
    worker = FakeWorker(env.client)
    r = env.client.put(
        "/api/v1/uploads", params={"filename": "live.mp4"}, content=video_bytes(tmp_path)
    )
    sid = r.json()["id"]
    started = env.client.post("/api/v1/sessions", json={"source_id": sid})
    assert started.status_code == 202
    session_id = started.json()["id"]
    uid = sid.removeprefix("upload-")
    busy = env.client.delete(f"/api/v1/uploads/{uid}")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "upload_in_use"
    assert worker.recv()["type"] == "session.start"
    env.client.post(f"/api/v1/sessions/{session_id}/stop")
    worker.close()
