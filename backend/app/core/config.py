"""Validated processing configuration loaded from ``config/config.json``.

All numeric values are *preliminary defaults*, not validated production values.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.paths import resolve_path


class ConfigError(Exception):
    """Raised when the configuration file is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideoConfig(_Section):
    source_type: Literal["file", "rtsp"] = "file"
    # File path or RTSP URL. RTSP URLs may embed credentials, so it is never exposed via the API.
    source: str = ""
    reconnect_interval_seconds: float = Field(default=5, gt=0, le=3600)


class DetectionConfig(_Section):
    frame_skip: int = Field(
        default=2, ge=0, le=300, description="Frames skipped between detections"
    )
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)


class RecognitionConfig(_Section):
    similarity_threshold: float = Field(default=0.55, ge=-1, le=1)


class TrackingConfig(_Section):
    max_missing_seconds: float = Field(default=2, gt=0, le=600)


class LoggingConfig(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    events_file: str = "logs/events.log"

    @field_validator("events_file")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("events_file must not be empty")
        return value


class AppConfig(_Section):
    video: VideoConfig = Field(default_factory=VideoConfig)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    recognition: RecognitionConfig = Field(default_factory=RecognitionConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @property
    def events_log_path(self) -> Path:
        return resolve_path(self.logging.events_file)


def load_app_config(path: Path) -> AppConfig:
    """Load and validate the config file.

    Rules: a missing file is an error (explicit beats silently using defaults); omitted
    sections/keys fall back to safe defaults; unknown keys are rejected.
    """
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Configuration file {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration file {path} must contain a JSON object")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
        raise ConfigError(f"Invalid configuration in {path}: {problems}") from exc
