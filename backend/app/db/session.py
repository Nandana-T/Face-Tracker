"""Engine / session lifecycle and database health checking."""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings

logger = logging.getLogger(__name__)


def create_db_engine(settings: Settings, connect_args: dict[str, Any] | None = None) -> Engine:
    """Build a pooled engine. Connections are lazy: no I/O happens here.

    TLS is controlled by the URL (``?sslmode=require`` for hosted databases).
    """
    args: dict[str, Any] = {"connect_timeout": settings.db_connect_timeout_seconds}
    if settings.db_use_pgbouncer:
        args["prepare_threshold"] = None  # server-side prepared statements break on PgBouncer
    if connect_args:
        args.update(connect_args)
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=settings.db_pool_recycle_seconds,
        connect_args=args,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(request: Request) -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed."""
    factory: sessionmaker[Session] = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()


@dataclass(frozen=True)
class DatabaseCheck:
    ok: bool
    latency_ms: float | None
    error: str | None = None


def check_database(engine: Engine, retries: int, backoff_seconds: float) -> DatabaseCheck:
    """Run ``SELECT 1``, retrying transient connection errors with linear backoff.

    The error string is a coarse category, never the driver message (which can contain hosts).
    """
    attempts = retries + 1
    last_error = "unavailable"
    for attempt in range(1, attempts + 1):
        start = time.perf_counter()
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return DatabaseCheck(True, round((time.perf_counter() - start) * 1000, 1))
        except OperationalError as exc:
            last_error = "connection_failed"
            logger.warning(
                "Database check failed (attempt %d/%d): %s",
                attempt,
                attempts,
                type(exc.orig).__name__,
            )
        except DBAPIError as exc:
            last_error = "query_failed"
            logger.warning(
                "Database check failed (attempt %d/%d): %s",
                attempt,
                attempts,
                type(exc.orig).__name__,
            )
        if attempt < attempts:
            time.sleep(backoff_seconds * attempt)
    return DatabaseCheck(False, None, last_error)
