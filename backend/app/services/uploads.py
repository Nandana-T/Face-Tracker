"""User-uploaded videos: validated, size-limited, stored under server-generated names.

Safety rules
* only authenticated users reach this (see ``api/v1/uploads.py``);
* the client's file name is NEVER used on disk: files are ``<uuid hex>.<ext>`` with the extension taken
  from an allow-list; the original name is kept only as a sanitised display label in a sidecar JSON;
* the body is streamed to a ``.part`` file with a hard byte cap (also checked against Content-Length);
* the finished file must really open as a video with a decodable first frame and sane dimensions,
  otherwise it is deleted - an arbitrary file renamed ``.mp4`` is refused;
* uploads are deleted by an authenticated ``DELETE`` (faces in them are biometric data) - nothing keeps
  them implicitly.
The video worker reads uploads through the same source registry, so this works when the API and the
worker share the upload folder (a local setup); with a remote worker the file would not be reachable.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import cv2

from app.core.config import VIDEO_EXTENSIONS, AppConfig
from app.core.paths import resolve_path

MAX_DIMENSION = 7680
_ID = re.compile(r"^[0-9a-f]{32}$")


class UploadError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def upload_dir(config: AppConfig) -> Path:
    return resolve_path(config.video.upload_dir)


def clean_label(filename: str) -> str:
    """A display label only: base name, no path, no control characters, bounded length."""
    base = re.split(r"[\\/]", filename)[-1]
    base = re.sub(r"[^\w .()\-+]", "_", base).strip(" ._")[:80]
    return base or "video"


def extension_of(filename: str) -> str:
    ext = Path(re.split(r"[\\/]", filename)[-1]).suffix.lower()
    if ext not in VIDEO_EXTENSIONS:
        raise UploadError(
            415, "unsupported_type", f"Supported video types: {', '.join(VIDEO_EXTENSIONS)}."
        )
    return ext


def new_upload_paths(config: AppConfig, filename: str) -> tuple[str, Path, Path]:
    ext = extension_of(filename)
    folder = upload_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    uid = uuid.uuid4().hex
    return uid, folder / f"{uid}{ext}.part", folder / f"{uid}{ext}"


def validate_video(path: Path) -> dict[str, float | int]:
    """The file must decode as a video: first frame readable, plausible size and frame rate."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise UploadError(422, "not_a_video", "The file could not be opened as a video.")
        ok, frame = cap.read()
        if not ok or frame is None or frame.size == 0:
            raise UploadError(422, "not_a_video", "The first frame could not be decoded.")
        h, w = frame.shape[:2]
        if not (16 <= w <= MAX_DIMENSION and 16 <= h <= MAX_DIMENSION):
            raise UploadError(422, "bad_dimensions", f"Video dimensions {w}x{h} are not supported.")
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        return {"width": w, "height": h, "fps": round(fps, 2), "frames": max(frames, 0)}
    finally:
        cap.release()


def write_sidecar(
    final: Path, uid: str, filename: str, size: int, info: dict[str, float | int]
) -> None:
    meta = {
        "id": uid,
        "label": clean_label(filename),
        "bytes": size,
        "uploaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **info,
    }
    final.with_suffix(".json").write_text(json.dumps(meta), encoding="utf-8")


def read_label(video: Path) -> str:
    try:
        meta = json.loads(video.with_suffix(".json").read_text(encoding="utf-8"))
        return clean_label(str(meta.get("label", "")))
    except (OSError, ValueError):
        return "video"


def find_upload(config: AppConfig, upload_id: str) -> Path | None:
    if not _ID.match(upload_id):
        return None
    folder = upload_dir(config)
    for ext in VIDEO_EXTENSIONS:
        candidate = folder / f"{upload_id}{ext}"
        if candidate.is_file():
            return candidate
    return None


def delete_upload(video: Path) -> None:
    video.unlink(missing_ok=True)
    video.with_suffix(".json").unlink(missing_ok=True)
