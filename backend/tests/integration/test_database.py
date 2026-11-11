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
    assert {"app_metadata", "processing_sessions", "alembic_version"} <= set(insp.get_table_names())
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
        "migration_revision": "0004",
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


def test_migrations_are_repeatable_and_never_destroy_existing_data(
    isolated_schema: tuple[Settings, Engine],
) -> None:
    """Upgrading again (or in steps) keeps rows written under earlier revisions."""
    _, engine = isolated_schema
    cfg = alembic_cfg(engine)
    command.upgrade(cfg, "0001")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO app_metadata (key, value) VALUES ('keep', 'me')"))

    command.upgrade(cfg, "0002")  # 0001 -> 0002 with data present
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO processing_sessions (id, source_id, source_label, source_type, state, "
                "frames_read, frames_processed, frames_skipped, frames_dropped, corrupt_frames, "
                "detections_total, max_faces_in_frame) VALUES "
                "('s1', 'a', 'A', 'file', 'COMPLETED', "
                "10, 4, 6, 0, 0, 3, 1)"
            )
        )

    command.upgrade(cfg, "0003")  # Phase 3 tables, then a visitor that 0004 must not disturb
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO visitors (id, visitor_number, registration_key, status, model_version, "
                "recognition_count) VALUES ('v1', 1, 's1:1', 'REGISTERED', 'sface', 0)"
            )
        )
    for _ in range(3):  # 0003 -> 0004 (Phase 4) with data present; re-running is a no-op
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM visitors")).scalar() == 1
        assert (
            conn.execute(text("SELECT value FROM app_metadata WHERE key = 'keep'")).scalar() == "me"
        )
        assert conn.execute(text("SELECT count(*) FROM processing_sessions")).scalar() == 1
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0004"
        assert {
            "visitors", "face_embeddings", "identity_associations",
            "visits", "visit_events", "event_delivery_outbox",
        } <= set(inspect(engine).get_table_names())  # fmt: skip

    # Rolling the newest migration back drops only what it created.
    command.downgrade(cfg, "0003")
    names = set(inspect(engine).get_table_names())
    assert not {"visits", "visit_events", "event_delivery_outbox"} & names
    assert "visitors" in names
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM visitors")).scalar() == 1
    command.downgrade(cfg, "0002")
    names = set(inspect(engine).get_table_names())
    assert not {"visitors", "face_embeddings", "identity_associations"} & names
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM processing_sessions")).scalar() == 1
    command.downgrade(cfg, "0001")
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT value FROM app_metadata WHERE key = 'keep'")).scalar() == "me"
        )
    command.upgrade(cfg, "head")
    assert "visitors" in inspect(engine).get_table_names()


def test_session_table_enforces_its_invariants_on_postgresql(
    isolated_schema: tuple[Settings, Engine],
) -> None:
    from sqlalchemy.exc import IntegrityError

    _, engine = isolated_schema
    command.upgrade(alembic_cfg(engine), "head")
    insert = (
        "INSERT INTO processing_sessions (id, source_id, source_label, source_type, state, "
        "active_slot, "
        "frames_read, frames_processed, frames_skipped, frames_dropped, corrupt_frames, "
        "detections_total, max_faces_in_frame) VALUES "
        "(:id, 'a', 'A', 'file', :state, :slot, 0,0,0,0,0,0,0)"
    )
    with engine.begin() as conn:
        conn.execute(text(insert), {"id": "a1", "state": "RUNNING", "slot": 1})
    with pytest.raises(IntegrityError), engine.begin() as conn:  # a second active session
        conn.execute(text(insert), {"id": "a2", "state": "STARTING", "slot": 1})
    with pytest.raises(IntegrityError), engine.begin() as conn:  # unknown state
        conn.execute(text(insert), {"id": "a3", "state": "BOGUS", "slot": None})
    with engine.begin() as conn:  # any number of finished sessions is fine
        conn.execute(text(insert), {"id": "a4", "state": "COMPLETED", "slot": None})
        conn.execute(text(insert), {"id": "a5", "state": "ERROR", "slot": None})


def test_tracking_counters_round_trip_through_postgresql_summary(
    isolated_schema: tuple[Settings, Engine],
) -> None:
    """Phase 2 needs no migration: the tracks_* counters live in the existing JSON `summary`."""
    from sqlalchemy.orm import sessionmaker

    from app.services.session_store import SessionStore

    _, engine = isolated_schema
    command.upgrade(alembic_cfg(engine), "head")
    store = SessionStore(sessionmaker(engine, expire_on_commit=False))
    row = store.create_active("clip", "Clip", "file", "worker-1", {"tracking_enabled": True})
    store.save_stats(
        row.id,
        {
            "frames_processed": 30,
            "tracks_created_total": 9,
            "tracks_confirmed_total": 4,
            "tracks_rejected_total": 3,
            "tracking_ms_avg": 0.31,
            "not_a_number": "dropped",
        },
    )
    got = store.get(row.id)
    assert got is not None and got.summary is not None
    assert got.summary["tracks_created_total"] == 9
    assert got.summary["tracks_rejected_total"] == 3
    assert got.summary["tracking_ms_avg"] == pytest.approx(0.31)
    assert "not_a_number" not in got.summary
    assert got.settings_snapshot == {"tracking_enabled": True}
    with engine.connect() as conn:  # and it is queryable as JSON in PostgreSQL itself
        raw = conn.execute(
            text(
                "SELECT summary->>'tracks_confirmed_total' FROM processing_sessions WHERE id = :i"
            ),
            {"i": row.id},
        ).scalar()
    assert raw == "4"
