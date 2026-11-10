"""YOLO11-nano face detector running on ONNX Runtime (CPU by default).

Preprocessing: letterbox to a square ``input_size`` (aspect ratio preserved, grey 114 padding,
centred), BGR->RGB, scale to [0, 1], NCHW float32.
Output ``(1, 5, N)``: ``cx, cy, w, h, face_confidence`` in letterboxed pixels (no objectness term,
sigmoid already applied). Boxes are mapped back to original-frame pixels, clipped, NMS'd.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.vision.detection.interfaces import (
    FaceDetection,
    InvalidFrame,
    ModelUnavailable,
)
from app.vision.sources.interfaces import Frame

logger = logging.getLogger("katomaran.detector")

PAD_VALUE = 114


def validate_pixels(pixels: Any) -> None:
    if (
        not isinstance(pixels, np.ndarray)
        or pixels.ndim != 3
        or pixels.shape[2] != 3
        or pixels.dtype != np.uint8
    ):
        raise InvalidFrame("Frame must be a (H, W, 3) uint8 BGR array.")
    h, w = pixels.shape[:2]
    if h < 2 or w < 2:
        raise InvalidFrame(f"Frame is too small ({w}x{h}).")


def letterbox(pixels: np.ndarray, size: int) -> tuple[np.ndarray, float, tuple[float, float]]:
    """Return (square image, scale, (pad_x, pad_y)) with original = (letterboxed - pad) / scale."""
    h, w = pixels.shape[:2]
    scale = min(size / h, size / w)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    resized = cv2.resize(pixels, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    pad_x, pad_y = (size - new_w) / 2, (size - new_h) / 2
    top, left = int(round(pad_y - 0.1)), int(round(pad_x - 0.1))
    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    canvas[top : top + new_h, left : left + new_w] = resized
    return canvas, scale, (float(left), float(top))


def to_tensor(square: np.ndarray) -> np.ndarray:
    rgb = cv2.cvtColor(square, cv2.COLOR_BGR2RGB)
    tensor = rgb.astype(np.float32) / 255.0
    return np.ascontiguousarray(tensor.transpose(2, 0, 1)[None])


def decode_detections(
    output: np.ndarray,
    *,
    conf_threshold: float,
    iou_threshold: float,
    scale: float,
    pad: tuple[float, float],
    frame_width: int,
    frame_height: int,
    max_faces: int,
) -> list[FaceDetection]:
    """Turn raw ``(1, 5, N)`` model output into NMS-filtered detections in original-frame pixels.

    A detection is kept when ``confidence >= conf_threshold`` (inclusive).
    """
    preds = np.asarray(output)
    if preds.ndim == 3:
        preds = preds[0]
    if preds.ndim != 2 or preds.shape[0] != 5:
        raise ModelUnavailable(
            f"Unexpected model output shape {tuple(np.asarray(output).shape)}; "
            "expected (1, 5, N) from a single-class YOLO11 face model."
        )
    preds = preds.T  # (N, 5)
    keep = preds[:, 4] >= conf_threshold
    preds = preds[keep]
    if preds.shape[0] == 0:
        return []
    cx, cy, bw, bh, conf = (preds[:, i] for i in range(5))
    x1 = np.clip((cx - bw / 2 - pad[0]) / scale, 0, frame_width)
    y1 = np.clip((cy - bh / 2 - pad[1]) / scale, 0, frame_height)
    x2 = np.clip((cx + bw / 2 - pad[0]) / scale, 0, frame_width)
    y2 = np.clip((cy + bh / 2 - pad[1]) / scale, 0, frame_height)
    valid = (x2 - x1 >= 1) & (y2 - y1 >= 1)
    x1, y1, x2, y2, conf = x1[valid], y1[valid], x2[valid], y2[valid], conf[valid]
    if conf.size == 0:
        return []
    boxes_xywh = np.stack([x1, y1, x2 - x1, y2 - y1], axis=1).tolist()
    # score_threshold is 0 here: confidence was already filtered with the inclusive comparison.
    idx = cv2.dnn.NMSBoxes(boxes_xywh, conf.tolist(), 0.0, float(iou_threshold))
    order = np.asarray(idx).reshape(-1).astype(int)
    order = order[np.argsort(-conf[order])][:max_faces]
    return [
        FaceDetection(
            x1=float(x1[i]),
            y1=float(y1[i]),
            x2=float(x2[i]),
            y2=float(y2[i]),
            confidence=float(min(1.0, max(0.0, conf[i]))),
        )
        for i in order
    ]


class YoloOnnxFaceDetector:
    def __init__(
        self,
        model_file: Path,
        *,
        input_size: int = 640,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        max_faces: int = 100,
        intra_op_threads: int = 0,
        allow_spinning: bool = True,
        name: str = "yolov11n-face",
    ) -> None:
        import onnxruntime as ort

        if not model_file.is_file():
            raise ModelUnavailable(
                f"Face detection model not found at {model_file}. "
                "Run scripts\\download-model.ps1 to download it."
            )
        options = ort.SessionOptions()
        if intra_op_threads > 0:
            options.intra_op_num_threads = intra_op_threads
        options.log_severity_level = 3
        if not allow_spinning:
            # Worker threads otherwise busy-wait between frames, burning CPU while idle.
            options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        started = time.perf_counter()
        try:
            self._session = ort.InferenceSession(
                str(model_file), sess_options=options, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # onnxruntime raises several unrelated exception types
            raise ModelUnavailable(
                f"The model file {model_file.name} could not be loaded ({type(exc).__name__}). "
                "It may be corrupt - delete it and re-run scripts\\download-model.ps1."
            ) from exc
        inp = self._session.get_inputs()[0]
        shape = inp.shape
        if len(shape) != 4 or shape[2] != input_size or shape[3] != input_size:
            raise ModelUnavailable(
                f"Model input shape {shape} does not match configured input_size={input_size}."
            )
        self._input_name = inp.name
        self.name = name
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.max_faces = max_faces
        self.load_seconds = time.perf_counter() - started
        self.model_bytes = model_file.stat().st_size
        self.last_inference_ms = 0.0
        logger.info(
            "Face detector loaded",
            extra={
                "model": name,
                "load_ms": round(self.load_seconds * 1000, 1),
                "threads": intra_op_threads or "auto",
                "providers": self._session.get_providers(),
            },
        )

    def detect(self, frame: Frame) -> list[FaceDetection]:
        pixels = frame.pixels
        validate_pixels(pixels)
        h, w = pixels.shape[:2]
        square, scale, pad = letterbox(pixels, self.input_size)
        tensor = to_tensor(square)
        started = time.perf_counter()
        output = self._session.run(None, {self._input_name: tensor})[0]
        self.last_inference_ms = (time.perf_counter() - started) * 1000
        return decode_detections(
            output,
            conf_threshold=self.conf_threshold,
            iou_threshold=self.iou_threshold,
            scale=scale,
            pad=pad,
            frame_width=w,
            frame_height=h,
            max_faces=self.max_faces,
        )

    def close(self) -> None:
        self._session = None
