"""Validated processing configuration loaded from ``config/config.json``.

All numeric values are *preliminary defaults*, not validated production values.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.paths import resolve_path


class ConfigError(Exception):
    """Raised when the configuration file is missing, unreadable or invalid."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


_SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VIDEO_EXTENSIONS = (".mp4", ".avi", ".mkv", ".mov", ".webm")


class SourceDef(_Section):
    """A trusted, server-side video source. The browser can only select one by ``id``.

    RTSP URLs commonly embed credentials, so they are never written in config.json: ``url_env``
    names the environment variable that holds the URL.
    """

    id: str
    label: str = Field(min_length=1, max_length=80)
    type: Literal["file", "rtsp"]
    path: str | None = None
    url_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _SOURCE_ID.match(value):
            raise ValueError("id must be lowercase letters, digits, '-' or '_' (max 64)")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> SourceDef:
        if self.type == "file" and (not self.path or self.url_env):
            raise ValueError("a file source needs 'path' and must not set 'url_env'")
        if self.type == "rtsp" and (not self.url_env or self.path):
            raise ValueError("an rtsp source needs 'url_env' and must not set 'path'")
        return self


class RtspConfig(_Section):
    connect_timeout_seconds: float = Field(default=10, gt=0, le=120)
    read_timeout_seconds: float = Field(default=10, gt=0, le=120)
    # 0 = keep retrying until stopped.
    max_reconnect_attempts: int = Field(default=0, ge=0, le=1000)
    max_backoff_seconds: float = Field(default=30, gt=0, le=3600)


class VideoConfig(_Section):
    # Legacy single-source shorthand: a non-empty ``source`` is exposed as source "default".
    source_type: Literal["file", "rtsp"] = "file"
    # File path or RTSP URL. RTSP URLs may embed credentials, so it is never exposed via the API.
    source: str = ""
    reconnect_interval_seconds: float = Field(default=5, gt=0, le=3600)
    # Every video file in this directory is offered as source "sample-<stem>".
    sample_dir: str = "datasets/samples"
    sources: list[SourceDef] = Field(default_factory=list)
    rtsp: RtspConfig = Field(default_factory=RtspConfig)
    # Consecutive undecodable frames tolerated before a file source is declared broken.
    max_consecutive_read_failures: int = Field(default=30, ge=1, le=10_000)

    @model_validator(mode="after")
    def _unique_ids(self) -> VideoConfig:
        ids = [s.id for s in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("video.sources ids must be unique")
        if "default" in ids and self.source.strip():
            raise ValueError("source id 'default' is reserved for video.source")
        return self


class DetectionConfig(_Section):
    # Exactly ``frame_skip`` frames are skipped between two detections: the detector runs on frame
    # indices 0, N+1, 2(N+1), ... (frame_skip=0 -> every frame, 2 -> every third frame).
    frame_skip: int = Field(
        default=2, ge=0, le=300, description="Frames skipped between detections"
    )
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    model: str = "yolov11n-face"
    # Optional override (relative to the models directory unless absolute). Default layout:
    # <models_dir>/face-detector/<model file>.
    model_path: str | None = None
    input_size: int = Field(default=640, ge=64, le=2048)
    iou_threshold: float = Field(default=0.45, gt=0, le=1)
    max_faces: int = Field(default=100, ge=1, le=1000)
    # 0 lets ONNX Runtime choose (it then uses every logical core for each inference).
    intra_op_threads: int = Field(default=0, ge=0, le=64)
    # ONNX Runtime threads busy-wait between frames by default, which burned ~70% of this machine's
    # CPU at a real-time 8 inferences/s. Off by default; see docs/benchmarks/phase1-benchmark.md.
    cpu_spin_wait: bool = False

    @model_validator(mode="after")
    def _model_compatible(self) -> DetectionConfig:
        from app.vision.detection.model_registry import MODELS

        spec = MODELS.get(self.model)
        if spec is None:
            raise ValueError(f"unknown detection model '{self.model}' (known: {sorted(MODELS)})")
        if self.input_size != spec.input_size:
            raise ValueError(
                f"model '{self.model}' has a fixed input size of {spec.input_size}; "
                f"got input_size={self.input_size}"
            )
        return self


class WorkerConfig(_Section):
    # Bounded hand-off between the reader thread and the inference thread.
    queue_size: int = Field(default=4, ge=1, le=64)
    # Pace file playback at the source frame rate (live monitoring). False = as fast as possible.
    realtime_pacing: bool = True
    heartbeat_interval_seconds: float = Field(default=5, gt=0, le=60)
    stats_interval_seconds: float = Field(default=1, gt=0, le=60)
    # API side: STARTING sessions that are not confirmed RUNNING within this time become ERROR.
    start_timeout_seconds: float = Field(default=30, gt=0, le=600)
    stop_timeout_seconds: float = Field(default=10, gt=0, le=120)


class StreamConfig(_Section):
    max_fps: float = Field(default=15, gt=0, le=60)
    max_width: int = Field(default=960, ge=160, le=3840)
    jpeg_quality: int = Field(default=70, ge=30, le=95)


class RecognitionConfig(_Section):
    """Face recognition and automatic registration (Phase 3). BIOMETRIC processing.

    Thresholds are cosine similarities between L2-normalised embeddings. Their defaults come from the
    LFW evaluation in docs/phase-3-report.md (not from other projects) and are deliberately
    conservative: merging two different people is worse than waiting for another observation.
    """

    enabled: bool = True
    model: str = "sface-2021dec"
    # Optional override (relative to the models directory unless absolute) for the embedder file.
    model_path: str | None = None
    # --- decision thresholds -------------------------------------------------------------------
    # best similarity >= this (and a clear margin) => the face IS that registered visitor
    similarity_threshold: float = Field(default=0.50, ge=-1, le=1)
    # best similarity < this => the face is NOT any registered visitor (candidate for registration).
    # Between the two the observation is "grey": no recognition, no registration - keep waiting.
    novelty_threshold: float = Field(default=0.40, ge=-1, le=1)
    # best - second-best visitor must exceed this, else the observation is AMBIGUOUS
    ambiguity_margin: float = Field(default=0.05, ge=0, le=1)
    # --- quality gate --------------------------------------------------------------------------
    min_face_px: int = Field(default=40, ge=16, le=512)
    min_detection_confidence: float = Field(default=0.6, ge=0, le=1)
    max_yaw: float = Field(default=0.45, gt=0, le=2)
    max_roll_degrees: float = Field(default=30, gt=0, le=90)
    min_sharpness: float = Field(default=15, ge=0, le=10000)
    min_quality_score: float = Field(default=0.3, ge=0, le=1)
    landmark_pad: float = Field(default=0.6, ge=0, le=3)
    # --- worker scheduling (embeddings are NOT computed for every face on every frame) --------
    observation_interval_seconds: float = Field(default=0.5, gt=0, le=60)
    warmup_observations: int = Field(default=6, ge=1, le=100)
    refresh_interval_seconds: float = Field(default=5, gt=0, le=3600)
    budget_ms_per_frame: float = Field(default=80, gt=0, le=5000)
    queue_size: int = Field(default=8, ge=1, le=256)
    crop_max_px: int = Field(default=192, ge=64, le=512)
    # --- identity resolution (API side) ---------------------------------------------------------
    vote_window: int = Field(default=8, ge=2, le=100)
    recognize_votes: int = Field(default=2, ge=1, le=20)
    switch_hysteresis: int = Field(default=2, ge=1, le=20)
    # registration: first sufficiently reliable observations, all of them novel and mutually consistent
    register_min_observations: int = Field(default=3, ge=1, le=50)
    register_min_seconds: float = Field(default=0.8, ge=0, le=600)
    register_min_quality: float = Field(default=0.35, ge=0, le=1)
    register_self_consistency: float = Field(default=0.6, ge=-1, le=1)
    max_embeddings_per_visitor: int = Field(default=5, ge=1, le=50)
    index_sync_seconds: float = Field(default=30, gt=0, le=3600)
    # --- storage / privacy -----------------------------------------------------------------------
    faces_dir: str = "storage/faces"
    retention_days: int = Field(default=0, ge=0, le=36500)  # 0 = keep until deleted via the API

    @model_validator(mode="after")
    def _thresholds_ordered(self) -> RecognitionConfig:
        if self.novelty_threshold > self.similarity_threshold:
            raise ValueError("novelty_threshold must not exceed similarity_threshold")
        return self


class TrackingConfig(_Section):
    """Temporary, session-scoped face tracks (never visitor identities). See
    docs/phase-2-report.md for the meaning and calibration of every value."""

    enabled: bool = True
    # Only detections scoring at least this can START a track; weaker ones may only extend one.
    high_confidence: float = Field(default=0.6, ge=0, le=1)
    # Observed updates needed for TENTATIVE -> CONFIRMED.
    confirm_hits: int = Field(default=3, ge=1, le=30)
    # A TENTATIVE track with no detection for this long is dropped (a "rejected" track).
    tentative_timeout_seconds: float = Field(default=0.5, gt=0, le=60)
    # A LOST track that is not re-detected within this time is REMOVED (the id is never reused).
    max_missing_seconds: float = Field(default=2, gt=0, le=600)
    # Minimum IoU between a track's predicted box and a detection to associate them.
    match_iou: float = Field(default=0.2, gt=0, le=1)
    low_match_iou: float = Field(default=0.3, gt=0, le=1)
    # Lower gate for re-acquiring a LOST track (its predicted position is less certain).
    # Association uses IoU of boxes grown by this fraction of their size per side (buffered IoU);
    # it keeps very small, fast faces matchable. 0 = plain IoU.
    box_buffer: float = Field(default=0.1, ge=0, le=1)
    lost_match_iou: float = Field(default=0.1, gt=0, le=1)
    # Per-second damping of a LOST track's velocity (0 = keep extrapolating at full speed).
    lost_velocity_decay_per_second: float = Field(default=1.0, ge=0, le=20)
    max_tracks: int = Field(default=200, ge=1, le=5000)


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
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    stream: StreamConfig = Field(default_factory=StreamConfig)
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
