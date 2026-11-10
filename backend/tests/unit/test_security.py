"""Access control: tokens, tickets, protected routes, production guards."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import Engine
from starlette.websockets import WebSocketDisconnect

from app.core.errors import AppError
from app.core.security import AuthService, sign, verify
from app.main import create_app
from app.workers import protocol as P
from tests.conftest import make_full_config, make_settings
from tests.unit.test_sessions_api import (
    AUTH as WORKER_AUTH,
)
from tests.unit.test_sessions_api import (
    TOKEN as WORKER_TOKEN,
)
from tests.unit.test_sessions_api import (
    Env,
    FakeWorker,
    api,
    running_session,
    wait_for,
)
from tests.unit.test_sources import write_video

PASSWORD = "correct horse battery staple"


def settings_with_auth(tmp_path: Path, **over: Any) -> Any:
    values: dict[str, Any] = {
        "admin_password": SecretStr(PASSWORD),
        "auth_secret": SecretStr("s" * 40),
        "worker_token": SecretStr(WORKER_TOKEN),
    }
    values.update(over)
    return make_settings(tmp_path, **values)


def build_app(tmp_path: Path, engine: Engine, **over: Any) -> Any:
    video = write_video(tmp_path / "clip.mp4")
    cfg = make_full_config(
        tmp_path,
        video={
            "sample_dir": str(tmp_path / "none"),
            "sources": [{"id": "clip", "label": "Clip", "type": "file", "path": str(video)}],
        },
    )
    return create_app(settings=settings_with_auth(tmp_path, **over), config=cfg, engine=engine)


def login(client: TestClient, password: str = PASSWORD) -> Any:
    return client.post("/api/v1/auth/login", json={"password": password})


def signed_in(client: TestClient) -> str:
    token: str = login(client).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    return token


# ---- token primitives ----------------------------------------------------------------------
SECRET = b"k" * 32


def test_tokens_verify_and_expire() -> None:
    token = sign(SECRET, {"kind": "access", "exp": 1000})
    assert verify(SECRET, token, "access", now=999) is not None
    assert verify(SECRET, token, "access", now=1000) is None  # expired (exclusive)


def test_tokens_reject_tampering_wrong_key_wrong_kind_and_junk() -> None:
    token = sign(SECRET, {"kind": "access", "exp": 10**10})
    body, mac = token.split(".")
    forged = sign(SECRET, {"kind": "access", "exp": 10**10, "sub": "root"}).split(".")[0]
    assert verify(b"x" * 32, token, "access") is None
    assert verify(SECRET, token, "stream") is None  # a login token is not a stream ticket
    assert verify(SECRET, f"{forged}.{mac}", "access") is None  # payload swapped, MAC reused
    for junk in ("", "abc", "a.b", f"{body}.", ".x", token + "x", "é.é"):
        assert verify(SECRET, junk, "access") is None


def test_unsigned_none_style_tokens_are_rejected() -> None:
    import base64

    payload = base64.urlsafe_b64encode(b'{"kind":"access","exp":9999999999}').rstrip(b"=").decode()
    assert verify(SECRET, f"{payload}.", "access") is None


# ---- login ---------------------------------------------------------------------------------
def test_login_succeeds_and_throttles_guessing(tmp_path: Path) -> None:
    auth = AuthService(settings_with_auth(tmp_path))
    token, ttl = auth.login(PASSWORD, "1.2.3.4", now=100)
    assert ttl == 8 * 3600
    assert verify(auth._secret, token, "access", now=101) is not None  # noqa: SLF001
    for _ in range(5):
        with pytest.raises(AppError) as bad:
            auth.login("wrong", "9.9.9.9", now=200)
        assert bad.value.status_code == 401
    with pytest.raises(AppError) as blocked:
        auth.login(PASSWORD, "9.9.9.9", now=201)  # even the right password is refused while blocked
    assert blocked.value.status_code == 429
    assert auth.login(PASSWORD, "9.9.9.9", now=200 + 301)[0]  # window passed
    assert auth.login(PASSWORD, "5.5.5.5", now=201)[0]  # other clients unaffected


def test_login_is_unavailable_when_auth_is_off(tmp_path: Path) -> None:
    auth = AuthService(make_settings(tmp_path))
    assert auth.required is False
    with pytest.raises(AppError) as exc:
        auth.login("x", "c")
    assert exc.value.code == "auth_disabled"


# ---- protected vs public routes ---------------------------------------------------------------
PROTECTED = [
    ("GET", "/api/v1/sources"),
    ("GET", "/api/v1/sources/clip"),
    ("GET", "/api/v1/sessions"),
    ("GET", "/api/v1/sessions/active"),
    ("GET", "/api/v1/sessions/" + "0" * 32),
    ("GET", "/api/v1/sessions/" + "0" * 32 + "/stats"),
    ("POST", "/api/v1/sessions"),
    ("POST", "/api/v1/sessions/" + "0" * 32 + "/stop"),
    ("POST", "/api/v1/sessions/" + "0" * 32 + "/stream-ticket"),
    ("GET", "/api/v1/system/status"),
    ("GET", "/api/v1/config"),
]


def test_every_non_public_route_requires_a_token(tmp_path: Path, sqlite_engine: Engine) -> None:
    with TestClient(build_app(tmp_path, sqlite_engine)) as client:
        for method, path in PROTECTED:
            r = client.request(
                method, path, json={"source_id": "clip"} if method == "POST" else None
            )
            assert r.status_code == 401, (method, path, r.status_code)
            assert r.json()["error"]["code"] == "unauthorized"
        # nothing was started by the rejected POST
        signed_in(client)
        assert client.get("/api/v1/sessions").json()["sessions"] == []


def test_public_routes_stay_public(tmp_path: Path, sqlite_engine: Engine) -> None:
    with TestClient(build_app(tmp_path, sqlite_engine)) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/api/v1/ready").status_code in (200, 503)
        status = client.get("/api/v1/auth/status").json()
        assert status == {"required": True, "authenticated": False}
        # public endpoints reveal nothing about sources, sessions or configuration
        assert "clip" not in client.get("/api/v1/ready").text


def test_valid_token_unlocks_the_api_and_bad_tokens_do_not(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    with TestClient(build_app(tmp_path, sqlite_engine)) as client:
        assert login(client, "nope").status_code == 401
        token = signed_in(client)
        assert client.get("/api/v1/sources").status_code == 200
        assert client.get("/api/v1/auth/status").json() == {"required": True, "authenticated": True}
        for header in (
            "Bearer " + token[:-2] + "xx",  # tampered
            "Bearer " + sign(b"other" * 8, {"kind": "access", "exp": 10**10}),  # wrong key
            "Bearer " + sign(b"s" * 40, {"kind": "access", "exp": time.time() - 5}),  # expired
            "Basic " + token,
            token,
        ):
            r = client.get("/api/v1/sources", headers={"Authorization": header})
            assert r.status_code == 401, header[:30]


def test_login_error_messages_do_not_leak_the_secret(tmp_path: Path, sqlite_engine: Engine) -> None:
    with TestClient(build_app(tmp_path, sqlite_engine)) as client:
        r = login(client, "wrong-password-value")
        assert PASSWORD not in r.text
        assert "wrong-password-value" not in r.text
        assert (
            client.post("/api/v1/auth/login", json={"password": "x", "extra": 1}).status_code == 422
        )


def test_login_endpoint_throttles_over_http(tmp_path: Path, sqlite_engine: Engine) -> None:
    with TestClient(build_app(tmp_path, sqlite_engine)) as client:
        codes = [login(client, f"guess-{i}").status_code for i in range(7)]
        assert codes[:5] == [401] * 5
        assert codes[5:] == [429, 429]
        assert login(client).status_code == 429  # blocked even with the right password


def test_auth_off_keeps_development_open(tmp_path: Path, sqlite_engine: Engine) -> None:
    cfg = make_full_config(tmp_path, video={"sample_dir": str(tmp_path / "none")})
    app = create_app(settings=make_settings(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        assert client.get("/api/v1/auth/status").json() == {
            "required": False,
            "authenticated": True,
        }
        assert client.get("/api/v1/sources").status_code == 200


# ---- stream tickets -------------------------------------------------------------------------
def test_stream_requires_a_valid_single_use_ticket_bound_to_the_session(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    with api(build_app(tmp_path, sqlite_engine)) as client:
        signed_in(client)
        env = Env(client, sqlite_engine)
        worker = FakeWorker(client)
        sid = running_session(env, worker)
        base = f"/api/v1/sessions/{sid}/stream"

        # no ticket / junk ticket / a login token used as a ticket: refused before any data
        for url in (
            base,
            base + "?ticket=junk",
            base + f"?ticket={client.headers['Authorization'][7:]}",
        ):
            with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(url):
                pass
            assert refused.value.code == 1008

        ticket = client.post(f"/api/v1/sessions/{sid}/stream-ticket").json()
        assert ticket["expires_in"] == 30
        with client.websocket_connect(f"{base}?ticket={ticket['ticket']}") as browser:
            assert json.loads(browser.receive_text())["type"] == "snapshot"
            worker.ws.send_bytes(
                P.pack_frame(
                    {"session_id": sid, "frame_id": 1, "faces": []}, b"\xff\xd8\xff\xe0jpeg"
                )
            )
            assert P.unpack_frame(browser.receive_bytes())[0]["frame_id"] == 1

        # single use: replaying the same ticket fails
        with (
            pytest.raises(WebSocketDisconnect) as replay,
            client.websocket_connect(f"{base}?ticket={ticket['ticket']}"),
        ):
            pass
        assert replay.value.code == 1008

        # bound to the session: a ticket for another session does not open this one
        other = client.post("/api/v1/sessions/" + "1" * 32 + "/stream-ticket")
        assert other.status_code == 404  # tickets are only issued for existing sessions
        worker.close()


def test_expired_tickets_are_rejected(tmp_path: Path, sqlite_engine: Engine) -> None:
    auth = AuthService(settings_with_auth(tmp_path))
    ticket, ttl = auth.issue_ticket("s1", now=1000)
    assert auth.consume_ticket(ticket, "s1", now=1000 + ttl + 1) is False
    ticket2, _ = auth.issue_ticket("s1", now=1000)
    assert auth.consume_ticket(ticket2, "s2", now=1001) is False  # wrong session
    assert auth.consume_ticket(ticket2, "s1", now=1001) is True
    assert auth.consume_ticket(ticket2, "s1", now=1002) is False  # replay
    assert auth.consume_ticket(None, "s1") is False


def test_worker_socket_is_independent_of_the_admin_password(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    with api(build_app(tmp_path, sqlite_engine)) as client:
        # the admin login token must NOT be accepted as the worker credential
        token = login(client).json()["access_token"]
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/v1/worker/ws", headers={"Authorization": f"Bearer {token}"}
            ),
        ):
            pass
        # ... while the worker token (no admin login needed) still connects
        with client.websocket_connect("/api/v1/worker/ws", headers=WORKER_AUTH) as ws:
            ws.send_text(
                json.dumps({"type": "hello", "worker_id": "w", "model": {"available": True}})
            )
            signed_in(client)
            wait_for(lambda: client.get("/api/v1/sources").json()["worker"]["connected"])


# ---- production guards ------------------------------------------------------------------------
GOOD_PROD: dict[str, Any] = {
    "app_env": "production",
    "admin_password": SecretStr("a-long-admin-password"),
    "auth_secret": SecretStr("x" * 40),
    "worker_token": SecretStr("w" * 32),
    "cors_origins": ["https://app.example.com"],
}


def test_production_refuses_unsafe_configuration(tmp_path: Path) -> None:
    ok = make_settings(tmp_path, **GOOD_PROD)
    assert ok.app_env == "production"
    cases: dict[str, list[Any]] = {
        "admin_password": [None, SecretStr("short")],
        "auth_secret": [None, SecretStr("short")],
        "worker_token": [None, SecretStr("short")],
        "cors_origins": [["http://localhost:5173"], ["http://app.example.com"], []],
    }
    for field, values in cases.items():
        for bad in values:
            with pytest.raises(ValidationError) as exc:
                make_settings(tmp_path, **{**GOOD_PROD, field: bad})
            assert "Unsafe production configuration" in str(exc.value), (field, bad)


def test_production_hides_interactive_docs_and_keeps_auth_on(
    tmp_path: Path, sqlite_engine: Engine
) -> None:
    cfg = make_full_config(tmp_path, video={"sample_dir": str(tmp_path / "none")})
    app = create_app(
        settings=make_settings(tmp_path, **GOOD_PROD), config=cfg, engine=sqlite_engine
    )
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 404
        assert client.get("/api/openapi.json").status_code == 404
        assert client.get("/api/v1/sources").status_code == 401
        assert client.get("/api/v1/auth/status").json()["required"] is True


def test_development_still_serves_docs(tmp_path: Path, sqlite_engine: Engine) -> None:
    cfg = make_full_config(tmp_path, video={"sample_dir": str(tmp_path / "none")})
    app = create_app(settings=make_settings(tmp_path), config=cfg, engine=sqlite_engine)
    with TestClient(app) as client:
        assert client.get("/api/docs").status_code == 200


def test_cors_rejects_unlisted_origins(tmp_path: Path, sqlite_engine: Engine) -> None:
    app = build_app(tmp_path, sqlite_engine, cors_origins=["https://app.example.com"])
    with TestClient(app) as client:
        evil = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in evil.headers
        good = client.get("/api/v1/health", headers={"Origin": "https://app.example.com"})
        assert good.headers["access-control-allow-origin"] == "https://app.example.com"
        assert "access-control-allow-credentials" not in good.headers  # bearer header, no cookies
