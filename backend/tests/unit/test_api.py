from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import UNREACHABLE_URL, make_config, make_settings


def test_health_ok_without_database(client_no_db: TestClient) -> None:
    r = client_no_db.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "service": "katomaran-api"}
    assert r.headers["x-request-id"]


def test_ready_reports_database_unavailable(client_no_db: TestClient) -> None:
    r = client_no_db.get("/api/v1/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["status"] == "unavailable"
    assert body["checks"]["database"]["error"] == "connection_failed"
    assert "127.0.0.1" not in r.text and "nobody" not in r.text  # no driver details leak


def test_system_status_when_db_down_is_error_envelope(client_no_db: TestClient) -> None:
    # The session dependency is lazy; the service reports the DB as unavailable honestly.
    r = client_no_db.get("/api/v1/system/status")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == {
        "status": "unavailable",
        "migration_revision": None,
        "migrations_applied": False,
    }
    assert body["components"]["video_worker"]["state"] == "not_running"
    assert body["components"]["face_detector"]["state"] == "not_running"
    # Phase 2/3: tracker and recognizer run in the worker, so without one they are "not running"
    assert body["components"]["face_recognizer"]["state"] == "not_running"
    assert body["components"]["tracker"]["state"] == "not_running"
    assert body["active_video_sources"] == 0


def test_not_found_error_structure(client_no_db: TestClient) -> None:
    r = client_no_db.get("/api/v1/nope", headers={"X-Request-ID": "abcdef123456"})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "not_found"
    assert err["request_id"] == "abcdef123456"
    assert set(err) == {"code", "message", "request_id", "details"}
    assert r.headers["x-request-id"] == "abcdef123456"


def test_invalid_request_id_is_replaced(client_no_db: TestClient) -> None:
    r = client_no_db.get("/api/v1/health", headers={"X-Request-ID": "bad id!"})
    assert r.headers["x-request-id"] != "bad id!"


def test_unhandled_exception_is_structured(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path), config=make_config(tmp_path))

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internals")

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/boom")
    app.state.engine.dispose()
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "internal_error"
    assert "secret internals" not in r.text
    assert err["request_id"]


def test_validation_error_structure(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path), config=make_config(tmp_path))

    @app.get("/needs-int")
    def needs_int(n: int) -> int:
        return n

    with TestClient(app) as client:
        r = client.get("/needs-int?n=abc")
    app.state.engine.dispose()
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert r.json()["error"]["details"][0]["loc"] == ["query", "n"]


def test_config_endpoint_redacts_sensitive_values(tmp_path: Path) -> None:
    secret_url = "rtsp://admin:hunter2@10.0.0.9/stream"
    settings = make_settings(tmp_path)
    app = create_app(
        settings=settings, config=make_config(tmp_path, source_type="rtsp", source=secret_url)
    )
    with TestClient(app) as client:
        r = client.get("/api/v1/config")
    app.state.engine.dispose()
    assert r.status_code == 200
    body = r.json()
    assert body["video"] == {
        "source_type": "rtsp",
        "source_configured": True,
        "reconnect_interval_seconds": 5.0,
    }
    for forbidden in ("hunter2", "10.0.0.9", "nobody", UNREACHABLE_URL, str(tmp_path), "events"):
        assert forbidden not in r.text


def test_openapi_exposes_versioned_routes(client_no_db: TestClient) -> None:
    paths = client_no_db.get("/api/openapi.json").json()["paths"]
    assert {
        "/api/v1/health",
        "/api/v1/ready",
        "/api/v1/system/status",
        "/api/v1/config",
    } <= set(paths)
    assert client_no_db.get("/api/docs").status_code == 200


def test_cors_allows_only_configured_origins(client_no_db: TestClient) -> None:
    ok = client_no_db.get("/api/v1/health", headers={"Origin": "http://localhost:5173"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:5173"
    bad = client_no_db.get("/api/v1/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_app_import_and_factory_do_not_touch_database(app_no_db: FastAPI) -> None:
    # Engine creation is lazy: constructing the app with an unreachable DB must succeed.
    assert app_no_db.state.engine.pool.checkedout() == 0
