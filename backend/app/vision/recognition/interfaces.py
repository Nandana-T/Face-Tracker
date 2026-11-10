"""Extension point: embedding + identity matching (InsightFace/ArcFace). Phase 2."""

from __future__ import annotations

from typing import Protocol

from app.vision.detection.interfaces import FaceDetection
from app.vision.sources.interfaces import Frame

Embedding = list[float]  # never logged


class FaceEmbedder(Protocol):
    def embed(self, frame: Frame, detection: FaceDetection) -> Embedding: ...


class IdentityMatcher(Protocol):
    def match(self, embedding: Embedding) -> tuple[int | None, float]:
        """Return ``(visitor_id or None, similarity)``."""
        ...
