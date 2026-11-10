"""Tracking metadata schemas (worker -> API frame headers, API -> browser).

Track ids are TEMPORARY and scoped to ``session_id`` (every frame header carries it). They are not
visitor identities and not unique across sessions. ``tracks`` always lists the complete current set
(TENTATIVE / CONFIRMED / LOST), so a client that connects mid-session needs no history.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

TRACKING_SCHEMA_VERSION = 1


class TrackOut(BaseModel):
    track_id: int = Field(ge=1)
    state: Literal["TENTATIVE", "CONFIRMED", "LOST"]
    bbox: tuple[int, int, int, int]
    # True only when a detection was matched to this track in THIS frame; otherwise bbox is a
    # motion prediction and `detection_confidence` is null (never invented).
    observed: bool
    detection_confidence: float | None = Field(default=None, ge=0, le=1)
    last_observed_confidence: float | None = Field(default=None, ge=0, le=1)
    first_seen_ms: int = Field(ge=0)
    last_seen_ms: int = Field(ge=0)
    age_ms: int = Field(ge=0)
    hits: int = Field(ge=1)
    missed_updates: int = Field(ge=0)
    last_update_frame: int = Field(ge=0)

    @model_validator(mode="after")
    def _consistent(self) -> TrackOut:
        x1, y1, x2, y2 = self.bbox
        if not (0 <= x1 < x2 and 0 <= y1 < y2):
            raise ValueError("bbox must satisfy 0 <= x1 < x2 and 0 <= y1 < y2")
        if self.observed and self.detection_confidence is None:
            raise ValueError("an observed track must carry its detection confidence")
        if not self.observed and self.detection_confidence is not None:
            raise ValueError("a predicted track must not carry a detection confidence")
        if self.state == "LOST" and self.observed:
            raise ValueError("a LOST track cannot be observed")
        return self


class TrackingSummary(BaseModel):
    """Counters at the time of this frame (see also the flat ``tracks_*`` session stats)."""

    schema_version: int = TRACKING_SCHEMA_VERSION
    enabled: bool = True
    tentative: int = Field(ge=0)
    confirmed: int = Field(ge=0)
    lost: int = Field(ge=0)
    created_total: int = Field(ge=0)
    confirmed_total: int = Field(ge=0)
    tracking_ms: float = Field(ge=0)


class TrackingFrameMeta(BaseModel):
    """The part of a frame header the API validates (extra header fields are ignored)."""

    session_id: str
    frame_id: int = Field(ge=0)
    frame_width: int = Field(gt=0)
    frame_height: int = Field(gt=0)
    tracks: list[TrackOut]
    tracking: TrackingSummary | None = None

    @model_validator(mode="after")
    def _inside_frame_and_unique(self) -> TrackingFrameMeta:
        seen: set[int] = set()
        for t in self.tracks:
            if t.bbox[2] > self.frame_width or t.bbox[3] > self.frame_height:
                raise ValueError("track bbox exceeds frame dimensions")
            if t.track_id in seen:
                raise ValueError("duplicate track id in one frame")
            seen.add(t.track_id)
        return self
