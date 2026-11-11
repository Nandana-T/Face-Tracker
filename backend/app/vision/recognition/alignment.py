"""Landmark-based face alignment to the 112x112 ArcFace/SFace template.

The YOLO box alone is not enough: the recognisers were trained on faces warped so that the eyes,
nose and mouth corners land on fixed positions. We estimate a similarity transform (rotation,
uniform scale, translation - no shear) from the 5 detected landmarks to that template.

Landmark order (image coordinates, the subject's right eye first): right eye, left eye, nose tip,
right mouth corner, left mouth corner.
"""

from __future__ import annotations

import cv2
import numpy as np

ALIGNED_SIZE = 112

# Standard ArcFace 112x112 template (same constants as InsightFace `arcface_dst` and OpenCV's
# FaceRecognizerSF).
TEMPLATE = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)


class AlignmentError(ValueError):
    pass


def similarity_transform(landmarks: np.ndarray) -> np.ndarray:
    """2x3 matrix mapping image landmarks onto the template (least squares, no shear)."""
    pts = np.asarray(landmarks, dtype=np.float32)
    if pts.shape != (5, 2) or not np.isfinite(pts).all():
        raise AlignmentError("expected 5 finite (x, y) landmarks")
    matrix, _ = cv2.estimateAffinePartial2D(pts, TEMPLATE, method=cv2.LMEDS)
    if matrix is None or not np.isfinite(matrix).all():
        raise AlignmentError("could not estimate an alignment transform")
    scale = float(np.hypot(matrix[0, 0], matrix[1, 0]))
    if not 0.05 < scale < 20:
        raise AlignmentError("degenerate alignment scale")
    return np.asarray(matrix, dtype=np.float32)


def align_face(pixels: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    """Return the aligned 112x112 BGR uint8 crop."""
    if pixels.ndim != 3 or pixels.shape[2] != 3 or pixels.size == 0:
        raise AlignmentError("expected a non-empty BGR image")
    matrix = similarity_transform(landmarks)
    out: np.ndarray = cv2.warpAffine(
        pixels,
        matrix,
        (ALIGNED_SIZE, ALIGNED_SIZE),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return out
