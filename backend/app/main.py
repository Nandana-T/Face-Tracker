"""FastAPI application factory.

Importing this module never loads AI models and never opens a database connection.
"""

from __future__ import annotations

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
from app.core.settings import Settings, get_settings
from app.db.session import create_db_engine, create_session_factory

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
        yield
        logger.info("Shutting down katomaran-api")
        if owns_engine:
            engine.dispose()

    app = FastAPI(
        title="Katomaran AI API",
        version=settings.app_version,
        description="Intelligent Face Tracker & Visitor Analytics",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.config = config
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.started_at = time.monotonic()

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
