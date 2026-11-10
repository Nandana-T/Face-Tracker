"""Detection metadata schemas (API-facing and worker->API frame headers).

Conventions
-----------
* ``frame_id``: 0-based index of the frame among frames delivered by the source.
* ``timestamp_ms``: milliseconds since the start of the source (files: ``frame_id / fps``;
  live streams: monotonic time since the stream was opened).
* ``bbox``: ``[x1, y1, x2, y2]`` integer pixels in the ORIGINAL frame (``frame_width`` x
  ``frame_height``), origin top-left, ``x1 < x2``, ``y1 < y2``, clipped to the frame.
* ``confidence``: face-detector score in ``[0, 1]``. It is NOT an identity score.
* A detection says "a face-like region is here in this frame" - nothing about who or whether the
  same person was seen before.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from app.schemas.tracking import TrackingSummary, TrackOut


class FaceBox(BaseModel):
    bbox: tuple[int, int, int, int]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> FaceBox:
        x1, y1, x2, y2 = self.bbox
        if not (0 <= x1 < x2 and 0 <= y1 < y2):
            raise ValueError("bbox must satisfy 0 <= x1 < x2 and 0 <= y1 < y2")
        return self


class DetectionResult(BaseModel):
    frame_id: int = Field(ge=0)
    timestamp_ms: int = Field(ge=0)
    frame_width: int = Field(gt=0)
    frame_height: int = Field(gt=0)
    faces: list[FaceBox]
    inference_ms: float = Field(ge=0)
    # Phase 2 (additive): null/empty when tracking is disabled. `faces` stays the raw detections.
    tracks: list[TrackOut] = Field(default_factory=list)
    tracking: TrackingSummary | None = None

    @model_validator(mode="after")
    def _inside_frame(self) -> DetectionResult:
        for face in self.faces:
            if face.bbox[2] > self.frame_width or face.bbox[3] > self.frame_height:
                raise ValueError("bbox exceeds frame dimensions")
        for track in self.tracks:
            if track.bbox[2] > self.frame_width or track.bbox[3] > self.frame_height:
                raise ValueError("track bbox exceeds frame dimensions")
        return self
