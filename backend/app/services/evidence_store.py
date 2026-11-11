"""Entry / exit face-crop evidence (biometric). NEVER served as static files.

Layout under the evidence root (default ``logs``):  ``entries/YYYY-MM-DD/<event_id>.jpg`` and
``exits/YYYY-MM-DD/<event_id>.jpg``. The file name is the stable event id, so delivering the same
event twice writes the same file (no duplicates); writes are temp-file + fsync + rename (never a
half-written file under the final name); references are validated and confined to the root.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

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
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def relative_path(event_type: str, event_id: str, when: datetime) -> str:
        folder = "entries" if event_type == "entry" else "exits"
        return f"{folder}/{when.strftime('%Y-%m-%d')}/{event_id}.jpg"

    def _resolve(self, relative: str) -> Path:
        if not _NAME.match(relative):
            raise ImageStoreError("invalid evidence reference")
        path = (self.root / relative).resolve()
        if self.root not in path.parents:
            raise ImageStoreError("path escapes the evidence root")
        return path

    def save(self, relative: str, jpeg: bytes) -> str:
        """Validate and store; returns the sha256 of the stored bytes. Idempotent: an existing file
        for the same event id is kept (an event's evidence never changes)."""
        path = self._resolve(relative)
        if path.is_file():
            return hashlib.sha256(path.read_bytes()).hexdigest()
        validate_evidence(jpeg)
        partial = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.part")  # unique per writer
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as handle:
                handle.write(jpeg)
                handle.flush()
                os.fsync(handle.fileno())
            partial.replace(path)
        except OSError as exc:
            partial.unlink(missing_ok=True)
            if path.is_file():  # a concurrent writer of the same event won the race: same evidence
                return hashlib.sha256(path.read_bytes()).hexdigest()
            raise EvidenceWriteError(f"could not write evidence: {exc}") from exc
        return hashlib.sha256(jpeg).hexdigest()

    def read(self, relative: str) -> bytes:
        try:
            return self._resolve(relative).read_bytes()
        except OSError as exc:
            raise ImageStoreError("evidence not found") from exc

    def exists(self, relative: str) -> bool:
        try:
            return self._resolve(relative).is_file()
        except ImageStoreError:
            return False

    def sha256(self, relative: str) -> str | None:
        try:
            return hashlib.sha256(self._resolve(relative).read_bytes()).hexdigest()
        except (OSError, ImageStoreError):
            return None

    def delete(self, relative: str) -> bool:
        path = self._resolve(relative)
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed

    def all_files(self) -> list[tuple[str, float]]:
        """(relative path, mtime) of every evidence file; stray ``.part`` files are listed too so
        reconciliation can remove them."""
        out: list[tuple[str, float]] = []
        for top in ("entries", "exits"):
            base = self.root / top
            if not base.is_dir():
                continue
            for day in base.iterdir():
                if not day.is_dir():
                    continue
                for f in day.iterdir():
                    if f.is_file():
                        out.append((f"{top}/{day.name}/{f.name}", f.stat().st_mtime))
        return out

    def delete_any(self, relative: str) -> None:
        """Delete a listed file even if it is a stray ``.part`` (path still confined)."""
        path = (self.root / relative).resolve()
        if self.root in path.parents:
            path.unlink(missing_ok=True)

    @staticmethod
    def age_seconds(mtime: float) -> float:
        return time.time() - mtime
