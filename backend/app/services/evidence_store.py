"""Entry / exit face-crop evidence (biometric). NEVER served as static files.

Layout under the evidence root (default ``logs``):  ``entries/YYYY-MM-DD/<event_id>.jpg`` and
``exits/YYYY-MM-DD/<event_id>.jpg``. The file name is the stable event id, so delivering the same
event twice writes the same file (no duplicates); writes are temp-file + fsync + rename (never a
half-written file under the final name); references are validated and confined to the root.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from app.services.blob_store import BlobBackend, BlobError, LocalBlobBackend
from app.services.image_store import ImageStoreError, validate_jpeg

_NAME = re.compile(r"^(entries|exits)/\d{4}-\d{2}-\d{2}/[0-9a-f]{32}\.jpg$")
MIN_SIDE = 32


class EvidenceRejected(ImageStoreError):
    """The image is not acceptable evidence (corrupt, tiny, blank). Retrying cannot help."""


class EvidenceWriteError(ImageStoreError):
    """The storage failed (disk full/unavailable). Retrying later may help."""


def validate_evidence(jpeg: bytes) -> np.ndarray:
    """Reject anything that is not a plausible face crop (corrupt, tiny, blank/black frame)."""
    try:
        pixels = validate_jpeg(jpeg)
    except ImageStoreError as exc:
        raise EvidenceRejected(str(exc)) from exc
    h, w = pixels.shape[:2]
    if min(h, w) < MIN_SIDE:
        raise EvidenceRejected("crop too small to be evidence")
    gray = cv2.cvtColor(pixels, cv2.COLOR_BGR2GRAY)
    if float(gray.mean()) < 8.0 or float(gray.std()) < 3.0:
        raise EvidenceRejected("crop is blank or black")
    return pixels


class EvidenceStore:
    """Entry/exit evidence on a ``BlobBackend`` (local folder or Supabase private storage)."""

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
    def relative_path(event_type: str, event_id: str, when: datetime) -> str:
        folder = "entries" if event_type == "entry" else "exits"
        return f"{folder}/{when.strftime('%Y-%m-%d')}/{event_id}.jpg"

    @staticmethod
    def _check(relative: str) -> None:
        if not _NAME.match(relative):
            raise ImageStoreError("invalid evidence reference")

    def save(self, relative: str, jpeg: bytes) -> str:
        """Validate and store; returns the sha256 of the stored bytes. Idempotent: an existing object
        for the same event id is kept (an event's evidence never changes)."""
        self._check(relative)
        try:
            if self.backend.exists(relative):
                return hashlib.sha256(self.backend.get(relative)).hexdigest()
        except BlobError as exc:
            raise EvidenceWriteError(f"could not reach the evidence store: {exc}") from exc
        validate_evidence(jpeg)
        try:
            self.backend.put(relative, jpeg)
        except BlobError as exc:
            raise EvidenceWriteError(f"could not write evidence: {exc}") from exc
        return hashlib.sha256(jpeg).hexdigest()

    def read(self, relative: str) -> bytes:
        self._check(relative)
        try:
            return self.backend.get(relative)
        except BlobError as exc:
            raise ImageStoreError("evidence not found") from exc

    def exists(self, relative: str) -> bool:
        try:
            self._check(relative)
            return self.backend.exists(relative)
        except (ImageStoreError, BlobError):
            return False

    def sha256(self, relative: str) -> str | None:
        try:
            self._check(relative)
            return hashlib.sha256(self.backend.get(relative)).hexdigest()
        except (ImageStoreError, BlobError):
            return None

    def delete(self, relative: str) -> bool:
        self._check(relative)
        try:
            return self.backend.delete(relative)
        except BlobError as exc:
            raise ImageStoreError(f"could not delete evidence: {exc}") from exc

    def all_files(self) -> list[tuple[str, float]]:
        """(relative path, mtime) of every evidence object; stray ``.part`` files (local only) are listed
        too so reconciliation can remove them."""
        out: list[tuple[str, float]] = []
        for top in ("entries", "exits"):
            try:
                out += self.backend.list(top)
            except BlobError as exc:
                raise ImageStoreError(f"could not list evidence: {exc}") from exc
        return out

    def delete_any(self, relative: str) -> None:
        """Delete a listed object even if it is a stray ``.part`` (still confined by the backend)."""
        with contextlib.suppress(BlobError):
            self.backend.delete(relative)

    @staticmethod
    def age_seconds(mtime: float) -> float:
        return time.time() - mtime
