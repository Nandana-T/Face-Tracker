"""Local face-image store (biometric data). NEVER served as static files.

Layout: ``<root>/<visitor_id>/<sha256[:20]>.jpg`` - content-addressed, so the same crop is never
stored twice. Files are written atomically (temp file + rename). Paths handed in by callers are
always resolved and checked to stay inside the root (no traversal). The store sits behind a small
interface so object storage can replace it later without touching the identity logic.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

import cv2
import numpy as np

_ID = re.compile(r"^[0-9a-f]{32}$")
_NAME = re.compile(r"^[0-9a-f]{32}/[0-9a-f]{20}\.jpg$")
MAX_IMAGE_BYTES = 512 * 1024


class ImageStoreError(Exception):
    pass


def validate_jpeg(data: bytes) -> np.ndarray:
    """Decode and sanity-check an untrusted JPEG; returns the pixels."""
    if not data.startswith(b"\xff\xd8") or len(data) > MAX_IMAGE_BYTES:
        raise ImageStoreError("not a JPEG or too large")
    pixels = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if pixels is None or pixels.ndim != 3:
        raise ImageStoreError("undecodable image")
    h, w = pixels.shape[:2]
    if not (16 <= h <= 1024 and 16 <= w <= 1024):
        raise ImageStoreError("unreasonable image dimensions")
    return pixels


class LocalImageStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, relative: str) -> Path:
        if not _NAME.match(relative):
            raise ImageStoreError("invalid image reference")
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise ImageStoreError("path escapes the image store")
        return path

    def save(self, visitor_id: str, jpeg: bytes) -> tuple[str, str]:
        """Validate and store; returns (relative path, sha256 of the bytes)."""
        if not _ID.match(visitor_id):
            raise ImageStoreError("invalid visitor id")
        validate_jpeg(jpeg)
        digest = hashlib.sha256(jpeg).hexdigest()
        relative = f"{visitor_id}/{digest[:20]}.jpg"
        path = self._resolve(relative)
        if path.is_file():
            return relative, digest  # same crop: nothing to write
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".part")
        try:
            with partial.open("wb") as handle:
                handle.write(jpeg)
                handle.flush()
                os.fsync(handle.fileno())
            partial.replace(path)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            raise ImageStoreError(f"could not write the image: {exc}") from exc
        return relative, digest

    def read(self, relative: str) -> bytes:
        path = self._resolve(relative)
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ImageStoreError("image not found") from exc

    def exists(self, relative: str) -> bool:
        try:
            return self._resolve(relative).is_file()
        except ImageStoreError:
            return False

    def delete_visitor(self, visitor_id: str) -> int:
        """Remove every image of one visitor (erasure). Returns the number of files deleted."""
        if not _ID.match(visitor_id):
            raise ImageStoreError("invalid visitor id")
        folder = (self.root / visitor_id).resolve()
        if self.root not in folder.parents or not folder.is_dir():
            return 0
        n = 0
        for f in folder.iterdir():
            if f.is_file():
                f.unlink(missing_ok=True)
                n += 1
        folder.rmdir()
        return n

    def delete(self, relative: str) -> None:
        self._resolve(relative).unlink(missing_ok=True)

    def all_files(self) -> list[tuple[str, float]]:
        """(relative path, mtime) of every stored image (for reconciliation)."""
        out: list[tuple[str, float]] = []
        for folder in self.root.iterdir():
            if folder.is_dir() and _ID.match(folder.name):
                for f in folder.iterdir():
                    rel = f"{folder.name}/{f.name}"
                    if f.is_file() and _NAME.match(rel):
                        out.append((rel, f.stat().st_mtime))
        return out

    @staticmethod
    def age_seconds(mtime: float) -> float:
        return time.time() - mtime
