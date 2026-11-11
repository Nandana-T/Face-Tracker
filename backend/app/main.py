"""FastAPI application factory.

Importing this module never loads AI models and never opens a database connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import Engine

from app.api.router import api_router
from app.core.config import AppConfig, load_app_config
from app.core.errors import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestIdMiddleware
from app.core.security import AuthService
from app.core.settings import Settings, get_settings
from app.db.session import create_db_engine, create_session_factory
from app.services.event_recorder import EventRecorder
from app.services.evidence_store import EvidenceStore
from app.services.identity_manager import IdentityManager
from app.services.image_store import LocalImageStore
from app.services.session_manager import SessionManager
from app.services.session_store import SessionStore
from app.services.visit_service import VisitService

logger = logging.getLogger("katomaran.app")


def create_app(
    settings: Settings | None = None,
    config: AppConfig | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    """Build the application. Dependencies may be injected (used by tests)."""
    settings = settings or get_settings()
    config = config or load_app_config(settings.config_path)  # fail fast on invalid config
    owns_engine = engine is None
    engine = engine or create_db_engine(settings)

    level = settings.log_level or config.logging.level
    setup_logging(level, settings.log_path, config.events_log_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "Starting katomaran-api",
            extra={"environment": settings.app_env, "version": settings.app_version},
        )
        sessions: SessionManager = app.state.sessions
        identity: IdentityManager | None = app.state.identity
        if identity is not None:
            try:
                n = await asyncio.to_thread(identity.load)
                await asyncio.to_thread(identity.reconcile)
                await asyncio.to_thread(identity.purge_expired)
                logger.info("Identity index loaded", extra={"embeddings": n})
            except Exception as exc:  # database may be down at boot; recognition retries lazily
                logger.warning("Could not load the identity index: %s", type(exc).__name__)
        visits: VisitService | None = app.state.visits
        try:
            await sessions.reconcile_orphans()
            if visits is not None:
                # Visits left open by a crashed/stopped process are closed as INTERRUPTED, log lines and
                # evidence interrupted mid-delivery are completed, orphaned files are removed.
                report = await asyncio.to_thread(visits.recorder.reconcile, None, True)
                logger.info("Event reconciliation", extra=report)
        except Exception as exc:  # database may be down at boot; the API must still start
            logger.warning("Could not reconcile orphaned sessions: %s", type(exc).__name__)
        monitor = asyncio.create_task(sessions.monitor_worker())
        if visits is not None:
            visits.start()
        yield
        logger.info("Shutting down katomaran-api")
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await monitor
        try:
            await sessions.shutdown()
        except Exception as exc:
            logger.warning("Session shutdown cleanup failed: %s", type(exc).__name__)
        if visits is not None:
            await visits.stop(config.visits.drain_timeout_seconds)
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="Katomaran AI API",
        version=settings.app_version,
        description="Intelligent Face Tracker & Visitor Analytics",
        # The interactive docs and schema describe every route: not served in production.
        docs_url=None if settings.app_env == "production" else "/api/docs",
        redoc_url=None,
        openapi_url=None if settings.app_env == "production" else "/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.config = config
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.started_at = time.monotonic()
    app.state.auth = AuthService(settings)
    app.state.sessions = SessionManager(config, SessionStore(app.state.session_factory))
    app.state.identity = None
    if config.recognition.enabled:
        from app.core.paths import resolve_path
        from app.vision.recognition.registry import SFACE

        app.state.identity = IdentityManager(
            app.state.session_factory,
            LocalImageStore(resolve_path(config.recognition.faces_dir)),
            config.recognition,
            SFACE.key,
            SFACE.embedding_dim,
        )
        app.state.sessions.identity = app.state.identity

    app.state.visits = None
    if config.visits.enabled:
        recorder = EventRecorder(
            app.state.session_factory, EvidenceStore(config.evidence_path), config.visits
        )
        app.state.visits = VisitService(config.visits, recorder)
        app.state.sessions.attach_visits(app.state.visits)

    register_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)  # outermost: wraps CORS and error responses
    app.include_router(api_router)
    return app


def app_factory() -> FastAPI:
    """Entry point for ``uvicorn app.main:app_factory --factory`` (no import-time side effects)."""
    return create_app()
