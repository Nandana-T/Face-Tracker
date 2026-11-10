"""Alembic environment. The URL always comes from application settings (DATABASE_URL).

Tests may inject a ready-made engine via ``config.attributes["engine"]`` (used to target an
isolated schema).
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import Engine

import app.models  # noqa: F401  (registers models on Base.metadata)
from alembic import context
from app.core.settings import get_settings
from app.db.base import Base
from app.db.session import create_db_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    injected = config.attributes.get("engine")
    engine: Engine = injected if injected is not None else create_db_engine(get_settings())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        if injected is None:
            engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
