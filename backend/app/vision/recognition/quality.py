"""Face-quality gate: is this observation good enough to identify - or register - someone?

A failed gate NEVER stops detection or tracking; the observation is simply not used (status
``INSUFFICIENT_QUALITY`` with machine-readable reasons). Every threshold is configurable
(``recognition.*`` in config.json) and the defaults are explained in docs/phase-3-report.md.

Checks (cheap, no extra model):
* size        - shorter side of the YOLO box in pixels
* detection   - YOLO confidence
* border      - box cut by the frame edge (partial face)
* landmarks   - YuNet found the 5 points inside the box region (alignment feasible)
* pose        - yaw / roll / pitch proxies from the landmarks (profile and looking-down faces embed badly)
* blur        - variance of the Laplacian of the aligned crop (focus / motion blur)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

INSUFFICIENT_QUALITY = "INSUFFICIENT_QUALITY"


@dataclass(frozen=True)
class QualityConfig:
    min_face_px: int = 40
    min_detection_confidence: float = 0.6
    max_yaw: float = 0.45  # |nose offset from the eye midpoint| / inter-eye distance
    max_roll_degrees: float = 30.0
    pitch_range: tuple[float, float] = (
        0.75,
        1.95,
    )  # (nose-eyes)/(mouth-nose); frontal LFW: 1.0-1.62 (5-95%)
    min_sharpness: float = (
        15.0  # Laplacian variance of the 112x112 grey crop (LFW blur study: docs/phase-3-report.md)
    )
    border_margin_px: int = 2


@dataclass(frozen=True)
class QualityResult:
    ok: bool
    score: float  # 0..1, higher is better; only meaningful when landmarks exist
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)


def pose_metrics(points: np.ndarray) -> tuple[float, float, float]:
    """(yaw proxy, roll degrees, pitch ratio) from 5 landmarks (right eye, left eye, nose, mouth R/L)."""
    re, le, nose, mr, ml = (points[i] for i in range(5))
    eye_vec = le - re
    eye_dist = float(np.linalg.norm(eye_vec))
    if eye_dist < 1e-3:
        return 9.9, 90.0, 0.0
    mid_eye = (re + le) / 2
    # Component of (nose - mid_eye) along the eye axis, relative to the eye distance.
    yaw = abs(float(np.dot(nose - mid_eye, eye_vec / eye_dist))) / eye_dist
    roll = abs(math.degrees(math.atan2(float(eye_vec[1]), float(eye_vec[0]))))
    mid_mouth = (mr + ml) / 2
    below = float(np.linalg.norm(mid_mouth - nose))
    above = float(np.linalg.norm(nose - mid_eye))
    pitch = above / below if below > 1e-3 else 9.9
    return yaw, roll, pitch


def sharpness(aligned_bgr: np.ndarray) -> float:
    grey = cv2.cvtColor(aligned_bgr, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var())


def assess(
    cfg: QualityConfig,
    box: tuple[float, float, float, float],
    detection_confidence: float,
    frame_size: tuple[int, int],
    landmarks: np.ndarray | None,
    aligned_bgr: np.ndarray | None,
) -> QualityResult:
    reasons: list[str] = []
    metrics: dict[str, float] = {}
    x1, y1, x2, y2 = box
    w, h = x2 - x1, y2 - y1
    side = min(w, h)
    metrics["face_px"] = round(side, 1)
    scores: list[float] = [min(1.0, side / (cfg.min_face_px * 2))]
    if side < cfg.min_face_px:
        reasons.append("too_small")
    metrics["detection_confidence"] = round(detection_confidence, 3)
    if detection_confidence < cfg.min_detection_confidence:
        reasons.append("low_detection_confidence")
    fw, fh = frame_size
    m = cfg.border_margin_px
    if x1 <= m or y1 <= m or x2 >= fw - m or y2 >= fh - m:
        reasons.append("cut_by_frame_edge")
    if landmarks is None or aligned_bgr is None:
        reasons.append("no_landmarks")
    else:
        yaw, roll, pitch = pose_metrics(landmarks)
        metrics.update(yaw=round(yaw, 3), roll=round(roll, 1), pitch=round(pitch, 3))
        if yaw > cfg.max_yaw:
            reasons.append("turned_away")
        if roll > cfg.max_roll_degrees and (180 - roll) > cfg.max_roll_degrees:
            reasons.append("tilted")
        if not cfg.pitch_range[0] <= pitch <= cfg.pitch_range[1]:
            reasons.append("looking_up_or_down")
        sharp = sharpness(aligned_bgr)
        metrics["sharpness"] = round(sharp, 1)
        if sharp < cfg.min_sharpness:
            reasons.append("blurry")
        scores += [
            max(0.0, 1 - yaw / (cfg.max_yaw * 1.5)),
            min(1.0, sharp / (cfg.min_sharpness * 3)),
        ]
    score = float(min(scores)) if scores else 0.0
    return QualityResult(not reasons, round(score, 3), tuple(reasons), metrics)
