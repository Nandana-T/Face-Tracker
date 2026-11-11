from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text

from app.core.config import AppConfig
from app.core.settings import Settings, get_settings
from app.db.base import Base
from app.db.session import create_db_engine
from app.main import create_app

# Nothing listens on port 1: connections are refused immediately (simulates DB outage).
UNREACHABLE_URL = "postgresql://nobody:nothing@127.0.0.1:1/none"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": SecretStr(UNREACHABLE_URL),
        "log_dir": tmp_path / "logs",
        "db_connect_retries": 0,
        "db_connect_timeout_seconds": 1,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_config(tmp_path: Path, **video: Any) -> AppConfig:
    return AppConfig.model_validate(
        {
            "video": video,
            "logging": {"events_file": str(tmp_path / "logs" / "events.log")},
            "recognition": {"faces_dir": str(tmp_path / "faces")},
            "visits": {"evidence_dir": str(tmp_path / "evidence")},
        }
    )


def make_full_config(tmp_path: Path, **sections: Any) -> AppConfig:
    """Config with arbitrary sections (video/detection/worker/stream...) plus a tmp events log."""
    data: dict[str, Any] = {"logging": {"events_file": str(tmp_path / "logs" / "events.log")}}
    data.update(sections)
    data.setdefault("recognition", {}).setdefault("faces_dir", str(tmp_path / "faces"))
    data.setdefault("visits", {}).setdefault("evidence_dir", str(tmp_path / "evidence"))
    return AppConfig.model_validate(data)


@pytest.fixture
def sqlite_engine(tmp_path: Path) -> Iterator[Engine]:
    """File-backed SQLite with the ORM schema: fast, no network, used for session-logic tests.

    A file (not ``:memory:`` + StaticPool) so each worker thread gets its own pooled connection;
    one shared connection is not safe under the concurrent ``to_thread`` calls the API makes.
    Migration correctness is verified separately against real PostgreSQL (integration tests).
    """
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'sessions.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def app_no_db(tmp_path: Path) -> Iterator[FastAPI]:
    """App whose database is unreachable."""
    settings = make_settings(tmp_path)
    app = create_app(settings=settings, config=make_config(tmp_path))
    yield app
    app.state.engine.dispose()


@pytest.fixture
def client_no_db(app_no_db: FastAPI) -> Iterator[TestClient]:
    with TestClient(app_no_db, raise_server_exceptions=False) as client:
        yield client


def _embedded_postgres_url() -> str:
    """Start (or reuse) the embedded local PostgreSQL in storage/pgdata and return its URL."""
    import pgserver

    from app.core.paths import project_root

    server = pgserver.get_server(project_root() / "storage" / "pgdata", cleanup_mode=None)  # type: ignore[attr-defined]
    return str(server.get_uri())


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """The database integration tests may use - never the development/production one.

    ``TEST_DATABASE_URL`` if set; otherwise the embedded local PostgreSQL. There is deliberately no
    fallback to ``DATABASE_URL`` any more, and a TEST_DATABASE_URL equal to it is refused.
    """
    from tests.db_safety import check_test_database

    url = os.environ.get("TEST_DATABASE_URL") or _embedded_postgres_url()
    check_test_database(
        url,
        get_settings().database_url.get_secret_value(),
        allow_hosted=os.environ.get("ALLOW_HOSTED_TEST_DB") == "1",
    )
    return url


@pytest.fixture
def isolated_schema(tmp_path: Path, test_database_url: str) -> Iterator[tuple[Settings, Engine]]:
    """A throw-away schema (``test_<hex>``) so tests never touch development/production tables.

    The schema is dropped afterwards. Failure to connect fails the test (never silently skipped).
    """
    schema = f"test_{uuid.uuid4().hex[:12]}"
    assert schema.startswith("test_")
    settings = make_settings(
        tmp_path,
        database_url=SecretStr(test_database_url),
        db_connect_timeout_seconds=20,
        db_connect_retries=2,
    )
    admin = create_db_engine(settings)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_db_engine(settings, connect_args={"options": f"-csearch_path={schema}"})
    try:
        yield settings, engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
