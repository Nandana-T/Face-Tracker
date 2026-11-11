"""Five-point facial landmarks from YuNet (MIT) via OpenCV's DNN module.

YuNet is used ONLY to locate landmarks inside the region the (already trusted) YOLO detector found;
it is not a second detector for the pipeline. It runs on a small padded crop, so its cost does not
grow with the frame resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# Crops smaller than this (px, longest side) are upscaled before YuNet sees them.
TARGET = 160.0


@dataclass(frozen=True)
class Landmarks:
    points: np.ndarray  # (5, 2) float32, frame coordinates
    score: float  # YuNet score of the face these points belong to


class LandmarkError(Exception):
    pass


class YuNetLandmarks:
    def __init__(self, model_path: Path, score_threshold: float = 0.5, pad: float = 0.35) -> None:
        if not model_path.is_file():
            raise LandmarkError(f"landmark model not found: {model_path}")
        self._pad = pad
        try:
            self._net = cv2.FaceDetectorYN.create(
                str(model_path), "", (320, 320), score_threshold, 0.3, 5
            )
        except cv2.error as exc:  # pragma: no cover - corrupt model
            raise LandmarkError(f"could not load the landmark model: {exc}") from exc

    def locate(
        self, pixels: np.ndarray, box: tuple[float, float, float, float]
    ) -> Landmarks | None:
        """Landmarks of the face that best matches ``box`` (x1, y1, x2, y2), or None."""
        h, w = pixels.shape[:2]
        x1, y1, x2, y2 = box
        bw, bh = x2 - x1, y2 - y1
        if bw < 4 or bh < 4:
            return None
        px, py = bw * self._pad, bh * self._pad
        cx1, cy1 = max(0, int(x1 - px)), max(0, int(y1 - py))
        cx2, cy2 = min(w, int(x2 + px + 1)), min(h, int(y2 + py + 1))
        crop = pixels[cy1:cy2, cx1:cx2]
        if crop.shape[0] < 8 or crop.shape[1] < 8:
            return None
        # YuNet needs a reasonably sized input to find small faces: upscale tiny crops.
        scale = max(1.0, TARGET / max(crop.shape[:2]))
        if scale > 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        self._net.setInputSize((crop.shape[1], crop.shape[0]))
        _, faces = self._net.detect(crop)
        if faces is None or len(faces) == 0:
            return None
        target = np.array([(x1 + x2) / 2 - cx1, (y1 + y2) / 2 - cy1]) * scale
        best = None
        best_dist = 1e18
        for row in faces:
            centre = np.array([row[0] + row[2] / 2, row[1] + row[3] / 2])
            dist = float(np.linalg.norm(centre - target))
            if dist < best_dist:
                best, best_dist = row, dist
        if best is None or best_dist > max(bw, bh) * scale * 0.6:
            return None  # the landmarks belong to some other face in the padded crop
        pts = np.asarray(best[4:14], dtype=np.float32).reshape(5, 2) / scale
        pts += np.array([cx1, cy1], dtype=np.float32)
        return Landmarks(pts, float(best[14]))
