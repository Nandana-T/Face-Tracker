"""Deployment settings, sourced from environment variables / ``.env``.

Settings hold *deployment* concerns (database URL, CORS, directories). Processing
parameters (thresholds, frame skipping) live in ``config/config.json`` - see ``config.py``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.paths import project_root, resolve_path

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=project_root() / ".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_version: str = "0.1.0"

    # --- Database -------------------------------------------------------------
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+psycopg://katomaran:katomaran@localhost:5432/katomaran"),
        description="SQLAlchemy/libpq URL. Hosted databases must use sslmode=require.",
    )
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_recycle_seconds: int = Field(default=1800, ge=30)
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_connect_retries: int = Field(default=2, ge=0, le=10)
    db_retry_backoff_seconds: float = Field(default=0.25, ge=0, le=10)
    # Set true when connecting through a transaction-mode pooler (PgBouncer / Supavisor :6543).
    db_use_pgbouncer: bool = False

    # --- HTTP -----------------------------------------------------------------
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    # --- Worker <-> API ---------------------------------------------------------
    # Shared secret the worker presents (Authorization: Bearer) when it connects to the API.
    # Unset = the worker endpoint refuses every connection.
    worker_token: SecretStr | None = None
    # Where the worker finds the API (ws:// or wss://). A deployment concern, so not in config.json.
    worker_api_url: str = "ws://127.0.0.1:8000"
    worker_id: str = "worker-1"

    # --- Access control (see app/core/security.py) -------------------------------
    # Setting ADMIN_PASSWORD turns authentication on (mandatory when APP_ENV=production).
    admin_password: SecretStr | None = None
    # Signs bearer tokens and stream tickets. Set it in production so tokens survive restarts.
    auth_secret: SecretStr | None = None
    auth_token_ttl_minutes: int = Field(default=480, ge=5, le=10_080)

    # --- Files / directories (relative paths resolve against the project root) ---
    config_file: Path = Path("config/config.json")
    log_dir: Path = Path("logs")
    storage_dir: Path = Path("storage")
    models_dir: Path = Path("models")
    log_level: LogLevel | None = Field(
        default=None, description="Overrides logging.level from config.json when set."
    )

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        """Refuse to start a production API that is open, weakly secured or CORS-misconfigured."""
        if self.app_env != "production":
            return self
        problems: list[str] = []
        if self.admin_password is None or len(self.admin_password.get_secret_value()) < 12:
            problems.append("ADMIN_PASSWORD (min 12 characters) is required")
        if self.auth_secret is None or len(self.auth_secret.get_secret_value()) < 32:
            problems.append("AUTH_SECRET (min 32 characters) is required")
        if self.worker_token is None or len(self.worker_token.get_secret_value()) < 24:
            problems.append("WORKER_TOKEN (min 24 characters) is required")
        insecure = [o for o in self.cors_origins if not o.startswith("https://")]
        if insecure or not self.cors_origins:
            problems.append("CORS_ORIGINS must list only https:// origins")
        if problems:
            raise ValueError("Unsafe production configuration: " + "; ".join(problems))
        return self

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                return json.loads(text)
            return [item.strip() for item in text.split(",") if item.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_in_production(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("CORS_ORIGINS must list explicit origins; '*' is not allowed")
        return value

    @field_validator("database_url")
    @classmethod
    def _normalise_driver(cls, value: SecretStr) -> SecretStr:
        """Accept ``postgresql://`` / ``postgres://`` and map to the psycopg 3 driver."""
        raw = value.get_secret_value()
        for prefix in ("postgresql://", "postgres://"):
            if raw.startswith(prefix):
                return SecretStr("postgresql+psycopg://" + raw[len(prefix) :])
        return value

    # Resolved absolute paths --------------------------------------------------
    @property
    def config_path(self) -> Path:
        return resolve_path(self.config_file)

    @property
    def log_path(self) -> Path:
        return resolve_path(self.log_dir)

    @property
    def storage_path(self) -> Path:
        return resolve_path(self.storage_dir)

    @property
    def models_path(self) -> Path:
        return resolve_path(self.models_dir)


@lru_cache
def get_settings() -> Settings:
    return Settings()
