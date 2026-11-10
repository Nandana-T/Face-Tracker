from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, Request
from sqlalchemy.exc import OperationalError

from app.db.session import check_database, create_db_engine, get_session
from tests.conftest import make_settings


def test_get_session_yields_and_always_closes(app_no_db: FastAPI) -> None:
    request = MagicMock(spec=Request)
    request.app = app_no_db
    gen = get_session(request)
    session = next(gen)
    closed = MagicMock(wraps=session.close)
    session.close = closed  # type: ignore[method-assign]
    gen.close()  # simulates request end / error path
    closed.assert_called_once()


def test_check_database_retries_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("app.db.session.time.sleep", sleeps.append)
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("SELECT 1", {}, Exception("down"))
    result = check_database(engine, retries=2, backoff_seconds=0.5)
    assert not result.ok and result.error == "connection_failed"
    assert engine.connect.call_count == 3
    assert sleeps == [0.5, 1.0]


def test_check_database_recovers_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.db.session.time.sleep", lambda _: None)
    engine = MagicMock()
    ok_conn = MagicMock()
    engine.connect.side_effect = [OperationalError("x", {}, Exception("blip")), ok_conn]
    assert check_database(engine, retries=1, backoff_seconds=0).ok


def test_engine_options(tmp_path: Path) -> None:
    s = make_settings(tmp_path, db_use_pgbouncer=True, db_pool_size=3)
    engine = create_db_engine(s)
    try:
        assert engine.pool.size() == 3  # type: ignore[attr-defined]
        assert engine.pool._pre_ping is True
    finally:
        engine.dispose()
