"""Resolves trusted, server-side source definitions into public descriptors and concrete sources.

The browser only ever sees ``SourceDescriptor`` (id, label, type, availability). Paths, RTSP URLs
and environment-variable names stay on the server.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.core.config import VIDEO_EXTENSIONS, AppConfig, SourceDef
from app.core.paths import resolve_path
from app.services.uploads import read_label
from app.vision.sources.file_source import FileVideoSource
from app.vision.sources.interfaces import SourceOpenError, VideoSource
from app.vision.sources.rtsp_source import RtspVideoSource

Availability = Literal["available", "unavailable"]


@dataclass(frozen=True)
class SourceDescriptor:
    id: str
    label: str
    type: Literal["file", "rtsp"]
    availability: Availability
    detail: str | None = None
    origin: Literal["config", "sample", "upload"] = "config"


@dataclass(frozen=True)
class _Resolved:
    descriptor: SourceDescriptor
    location: str  # filesystem path or RTSP URL (secret!)


def _slug(stem: str) -> str:
    cleaned = "".join(c if c.isalnum() else "-" for c in stem.lower()).strip("-")
    return cleaned[:50] or "video"


def _entries(config: AppConfig, env: Mapping[str, str]) -> list[_Resolved]:
    out: list[_Resolved] = []
    legacy = config.video.source.strip()
    if legacy:
        if config.video.source_type == "file":
            p = resolve_path(legacy)
            ok = p.is_file()
            out.append(
                _Resolved(
                    SourceDescriptor(
                        "default",
                        "Configured source",
                        "file",
                        "available" if ok else "unavailable",
                        None if ok else "The configured video file was not found.",
                    ),
                    str(p),
                )
            )
        else:
            out.append(
                _Resolved(
                    SourceDescriptor("default", "Configured camera", "rtsp", "available"), legacy
                )
            )
    for s in config.video.sources:
        out.append(_resolve_def(s, env))
    sample_dir = resolve_path(config.video.sample_dir)
    if sample_dir.is_dir():
        taken = {e.descriptor.id for e in out}
        for f in sorted(sample_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS:
                sid = f"sample-{_slug(f.stem)}"
                if sid not in taken:
                    taken.add(sid)
                    out.append(
                        _Resolved(
                            SourceDescriptor(
                                sid, f"Sample: {f.stem}", "file", "available", None, "sample"
                            ),
                            str(f),
                        )
                    )
    upload_dir = resolve_path(config.video.upload_dir)
    if config.video.uploads_enabled and upload_dir.is_dir():
        for f in sorted(upload_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (
                f.is_file()
                and f.suffix.lower() in VIDEO_EXTENSIONS
                and re.fullmatch(r"[0-9a-f]{32}", f.stem)
            ):
                out.append(
                    _Resolved(
                        SourceDescriptor(
                            f"upload-{f.stem}",
                            f"Upload: {read_label(f)}",
                            "file",
                            "available",
                            None,
                            "upload",
                        ),
                        str(f),
                    )
                )
    return out


def _resolve_def(s: SourceDef, env: Mapping[str, str]) -> _Resolved:
    if s.type == "file":
        assert s.path is not None
        p = resolve_path(s.path)
        ok = p.is_file()
        return _Resolved(
            SourceDescriptor(
                s.id,
                s.label,
                "file",
                "available" if ok else "unavailable",
                None if ok else "The video file was not found on the server.",
            ),
            str(p),
        )
    assert s.url_env is not None
    url = env.get(s.url_env, "").strip()
    ok = bool(url)
    return _Resolved(
        SourceDescriptor(
            s.id,
            s.label,
            "rtsp",
            "available" if ok else "unavailable",
            None if ok else "The camera address is not configured on the server.",
        ),
        url,
    )


def list_sources(config: AppConfig, env: Mapping[str, str] | None = None) -> list[SourceDescriptor]:
    return [e.descriptor for e in _entries(config, env if env is not None else os.environ)]


def get_descriptor(
    config: AppConfig, source_id: str, env: Mapping[str, str] | None = None
) -> SourceDescriptor | None:
    for e in _entries(config, env if env is not None else os.environ):
        if e.descriptor.id == source_id:
            return e.descriptor
    return None


def build_source(
    config: AppConfig,
    source_id: str,
    env: Mapping[str, str] | None = None,
    on_state: object = None,
) -> VideoSource:
    """Create (not open) the concrete source. Raises SourceOpenError for unknown/unusable ids."""
    for e in _entries(config, env if env is not None else os.environ):
        if e.descriptor.id != source_id:
            continue
        if e.descriptor.availability != "available":
            raise SourceOpenError(
                e.descriptor.detail or "Source unavailable.", "source_unavailable"
            )
        if e.descriptor.type == "file":
            return FileVideoSource(Path(e.location), config.video.max_consecutive_read_failures)
        r = config.video.rtsp
        return RtspVideoSource(
            e.location,
            connect_timeout_s=r.connect_timeout_seconds,
            read_timeout_s=r.read_timeout_seconds,
            reconnect_interval_s=config.video.reconnect_interval_seconds,
            max_backoff_s=r.max_backoff_seconds,
            max_reconnect_attempts=r.max_reconnect_attempts,
            on_state=on_state,  # type: ignore[arg-type]
        )
    raise SourceOpenError("Unknown video source.", "source_not_found")
