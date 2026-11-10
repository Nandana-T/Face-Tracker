"""Integration tests against a real PostgreSQL (TEST_DATABASE_URL, else DATABASE_URL).

Each test runs inside a throw-away schema; ``public`` data is never touched.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, text

from alembic import command
from app.core.settings import Settings
from app.main import create_app
from tests.conftest import make_config

pytestmark = pytest.mark.integration

BACKEND = Path(__file__).resolve().parents[2]


def alembic_cfg(engine: Engine) -> Config:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["engine"] = engine
    return cfg


def test_migrations_apply_and_roll_back_cleanly(isolated_schema: tuple[Settings, Engine]) -> None:
    _, engine = isolated_schema
    cfg = alembic_cfg(engine)
    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert {"app_metadata", "alembic_version"} <= set(insp.get_table_names())
    pk = insp.get_pk_constraint("app_metadata")
    assert pk["constrained_columns"] == ["key"]
    command.upgrade(cfg, "head")  # idempotent
    command.downgrade(cfg, "base")
    assert "app_metadata" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")  # reproducible round-trip


def test_models_match_migrations(isolated_schema: tuple[Settings, Engine]) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db.base import Base

    _, engine = isolated_schema
    command.upgrade(alembic_cfg(engine), "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_api_ready_and_status_with_real_database(
    isolated_schema: tuple[Settings, Engine], tmp_path: Path
) -> None:
    settings, engine = isolated_schema
    command.upgrade(alembic_cfg(engine), "head")
    app = create_app(settings=settings, config=make_config(tmp_path), engine=engine)
    with TestClient(app) as client:
        ready = client.get("/api/v1/ready")
        status = client.get("/api/v1/system/status")
    assert ready.status_code == 200
    assert ready.json()["checks"]["database"]["status"] == "ok"
    assert status.json()["database"] == {
        "status": "ok",
        "migration_revision": "0001",
        "migrations_applied": True,
    }


def test_status_reports_unapplied_migrations(
    isolated_schema: tuple[Settings, Engine], tmp_path: Path
) -> None:
    settings, engine = isolated_schema
    app = create_app(settings=settings, config=make_config(tmp_path), engine=engine)
    with TestClient(app) as client:
        body = client.get("/api/v1/system/status").json()
    assert body["database"]["status"] == "ok"
    assert body["database"]["migrations_applied"] is False


def test_session_lifecycle_returns_connection_to_pool(
    isolated_schema: tuple[Settings, Engine], tmp_path: Path
) -> None:
    settings, engine = isolated_schema
    app = create_app(settings=settings, config=make_config(tmp_path), engine=engine)
    with TestClient(app) as client:
        for _ in range(10):
            assert client.get("/api/v1/system/status").status_code == 200
    assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]


def test_tls_in_use_for_hosted_connections(isolated_schema: tuple[Settings, Engine]) -> None:
    settings, engine = isolated_schema
    host = settings.database_url.get_secret_value()
    with engine.connect() as conn:
        ssl = conn.execute(
            text("SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
        ).scalar()
    if "supabase" in host or "sslmode=require" in host:
        assert ssl is True
    logging.getLogger(__name__).info("ssl=%s", ssl)
