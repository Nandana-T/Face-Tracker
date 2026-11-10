"""Pure detector logic with synthetic model outputs (no model weights involved).

These tests prove geometry/thresholding/NMS correctness, NOT detection accuracy - see
tests/integration/test_detector_real.py for the real model.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.vision.detection.interfaces import FaceDetection, InvalidFrame, ModelUnavailable
from app.vision.detection.model_registry import (
    YOLOV11N_FACE,
    ModelFetchError,
    sha256_of,
    verify_model_file,
)
from app.vision.detection.yolo_onnx import (
    YoloOnnxFaceDetector,
    decode_detections,
    letterbox,
    to_tensor,
    validate_pixels,
)


def output(rows: Sequence[tuple[float, float, float, float, float]]) -> np.ndarray:
    """Build a (1, 5, N) model output from (cx, cy, w, h, conf) rows."""
    arr = np.array(rows, dtype=np.float32).T if rows else np.zeros((5, 0), dtype=np.float32)
    return arr[None]


def decode(
    rows: Sequence[tuple[float, float, float, float, float]],
    conf: float = 0.5,
    scale: float = 1.0,
    pad: tuple[float, float] = (0.0, 0.0),
    w: int = 640,
    h: int = 640,
    **kw: Any,
) -> list[FaceDetection]:
    params: dict[str, Any] = {"iou_threshold": 0.45, "max_faces": 100, **kw}
    return decode_detections(
        output(rows),
        conf_threshold=conf,
        scale=scale,
        pad=pad,
        frame_width=w,
        frame_height=h,
        **params,
    )


def test_no_face_frame_returns_empty() -> None:
    assert decode([]) == []
    assert decode([(100, 100, 50, 50, 0.1)]) == []


def test_single_face_geometry() -> None:
    (d,) = decode([(320, 240, 100, 120, 0.9)])
    assert (d.x1, d.y1, d.x2, d.y2) == (270, 180, 370, 300)
    assert d.confidence == pytest.approx(0.9)


def test_multi_face_sorted_by_confidence() -> None:
    dets = decode([(100, 100, 60, 60, 0.7), (400, 400, 60, 60, 0.95), (250, 500, 60, 60, 0.6)])
    assert [round(d.confidence, 2) for d in dets] == [0.95, 0.7, 0.6]


@pytest.mark.parametrize(("conf", "kept"), [(0.5, 1), (0.50001, 0), (0.49, 1), (0.0, 1), (1.0, 0)])
def test_confidence_threshold_edges_are_inclusive(conf: float, kept: int) -> None:
    assert len(decode([(100, 100, 50, 50, 0.5)], conf=conf)) == kept


def test_nms_suppresses_overlapping_boxes_but_keeps_distinct() -> None:
    dets = decode([(100, 100, 80, 80, 0.9), (104, 102, 80, 80, 0.8), (400, 400, 80, 80, 0.7)])
    assert len(dets) == 2
    assert dets[0].confidence == pytest.approx(0.9)


def test_max_faces_cap() -> None:
    rows = [(60 + 70 * i, 100, 40, 40, 0.9 - i * 0.01) for i in range(8)]
    assert len(decode(rows, max_faces=3)) == 3


def test_boxes_clipped_to_frame_and_degenerate_dropped() -> None:
    (d,) = decode([(5, 5, 100, 100, 0.9)], w=200, h=100)
    assert (d.x1, d.y1) == (0, 0)
    assert d.x2 <= 200 and d.y2 <= 100
    assert decode([(700, 700, 40, 40, 0.9)], w=640, h=640) == []  # fully outside


def test_letterbox_mapping_is_invertible() -> None:
    """A box in original space survives letterbox -> model space -> decode."""
    frame = np.zeros((360, 640, 3), dtype=np.uint8)  # 16:9 -> vertical padding
    square, scale, pad = letterbox(frame, 640)
    assert square.shape == (640, 640, 3)
    assert scale == pytest.approx(1.0)
    assert pad[0] == 0
    assert pad[1] == pytest.approx(140, abs=1)
    # Face at x 100..200, y 50..150 in the ORIGINAL frame; model space adds the padding.
    cx, cy = 150 * scale + pad[0], 100 * scale + pad[1]
    (d,) = decode([(cx, cy, 100 * scale, 100 * scale, 0.9)], scale=scale, pad=pad, w=640, h=360)
    assert (d.x1, d.y1, d.x2, d.y2) == pytest.approx((100, 50, 200, 150), abs=1.5)


def test_letterbox_downscale_and_tensor_layout() -> None:
    frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
    square, scale, _pad = letterbox(frame, 640)
    assert scale == pytest.approx(1 / 3)
    tensor = to_tensor(square)
    assert tensor.shape == (1, 3, 640, 640)
    assert tensor.dtype == np.float32
    assert tensor.min() >= 0.0 and tensor.max() <= 1.0


def test_unexpected_output_shape_is_reported() -> None:
    with pytest.raises(ModelUnavailable, match="Unexpected model output"):
        decode_detections(
            np.zeros((1, 84, 100), dtype=np.float32),
            conf_threshold=0.5,
            iou_threshold=0.5,
            scale=1,
            pad=(0, 0),
            frame_width=10,
            frame_height=10,
            max_faces=5,
        )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "frame",
        np.zeros((0, 0, 3), dtype=np.uint8),
        np.zeros((10, 10), dtype=np.uint8),
        np.zeros((10, 10, 4), dtype=np.uint8),
        np.zeros((10, 10, 3), dtype=np.float32),
        np.zeros((1, 50, 3), dtype=np.uint8),
    ],
)
def test_invalid_frames_rejected(bad: object) -> None:
    with pytest.raises(InvalidFrame):
        validate_pixels(bad)


# ---- model management ------------------------------------------------------------------------
def test_missing_model_gives_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(ModelUnavailable, match="download-model"):
        YoloOnnxFaceDetector(tmp_path / "absent.onnx")
    with pytest.raises(ModelFetchError, match="download-model"):
        verify_model_file(tmp_path / "absent.onnx", YOLOV11N_FACE)


def test_corrupt_model_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"this is not an onnx model")
    with pytest.raises(ModelUnavailable, match="could not be loaded"):
        YoloOnnxFaceDetector(bad)
    with pytest.raises(ModelFetchError, match="checksum"):
        verify_model_file(bad, YOLOV11N_FACE)
    assert sha256_of(bad) != YOLOV11N_FACE.sha256


def test_model_spec_is_pinned() -> None:
    assert len(YOLOV11N_FACE.sha256) == 64
    assert YOLOV11N_FACE.revision in YOLOV11N_FACE.url  # immutable commit, not "main"
