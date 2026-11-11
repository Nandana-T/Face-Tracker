"""Local face-image store (biometric data). NEVER served as static files.

Layout: ``<root>/<visitor_id>/<sha256[:20]>.jpg`` - content-addressed, so the same crop is never
stored twice. Files are written atomically (temp file + rename). Paths handed in by callers are
always resolved and checked to stay inside the root (no traversal). The store sits behind a small
interface so object storage can replace it later without touching the identity logic.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path

import cv2
import numpy as np

from app.services.blob_store import BlobBackend, BlobError, LocalBlobBackend

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
    """Visitor representative images on a ``BlobBackend`` (local folder by default, or Supabase private
    storage). The name is historical: pass a ``Path`` for the local filesystem or a ready backend."""

    def __init__(self, root: Path | None = None, backend: BlobBackend | None = None) -> None:
        if backend is None:
            if root is None:
                raise ValueError("a root folder or a backend is required")
            backend = LocalBlobBackend(root)
        self.backend = backend

    @property
    def root(self) -> Path:
        """The local folder (local backend only; remote storage has none)."""
        root: Path | None = getattr(self.backend, "root", None)
        if root is None:
            raise RuntimeError("remote storage has no local root")
        return root

    @staticmethod
    def _check(relative: str) -> None:
        if not _NAME.match(relative):
            raise ImageStoreError("invalid image reference")

    def save(self, visitor_id: str, jpeg: bytes) -> tuple[str, str]:
        """Validate and store; returns (relative path, sha256 of the bytes). Idempotent."""
        if not _ID.match(visitor_id):
            raise ImageStoreError("invalid visitor id")
        validate_jpeg(jpeg)
        digest = hashlib.sha256(jpeg).hexdigest()
        relative = f"{visitor_id}/{digest[:20]}.jpg"
        try:
            self.backend.put(relative, jpeg)  # an existing object is left alone
        except BlobError as exc:
            raise ImageStoreError(f"could not write the image: {exc}") from exc
        return relative, digest

    def read(self, relative: str) -> bytes:
        self._check(relative)
        try:
            return self.backend.get(relative)
        except BlobError as exc:
            raise ImageStoreError("image not found") from exc

    def exists(self, relative: str) -> bool:
        try:
            self._check(relative)
            return self.backend.exists(relative)
        except (ImageStoreError, BlobError):
            return False

    def delete_visitor(self, visitor_id: str) -> int:
        """Remove every image of one visitor (erasure). Returns the number of objects deleted."""
        if not _ID.match(visitor_id):
            raise ImageStoreError("invalid visitor id")
        try:
            return self.backend.delete_prefix(f"{visitor_id}/")
        except BlobError as exc:
            raise ImageStoreError(f"could not erase the images: {exc}") from exc

    def delete(self, relative: str) -> None:
        self._check(relative)
        try:
            self.backend.delete(relative)
        except BlobError as exc:
            raise ImageStoreError(f"could not delete the image: {exc}") from exc

    def all_files(self) -> list[tuple[str, float]]:
        """(relative path, mtime) of every stored image (for reconciliation)."""
        try:
            return [(k, m) for k, m in self.backend.list("") if _NAME.match(k)]
        except BlobError as exc:
            raise ImageStoreError(f"could not list the images: {exc}") from exc

    @staticmethod
    def age_seconds(mtime: float) -> float:
        return time.time() - mtime
