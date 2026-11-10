"""Real-model tests: the downloaded YOLO face detector on a licensed real photograph.

Requires the model (scripts\\download-model.ps1). They fail loudly - never skip - when it is
missing, so a green run really means the real detector ran. The image is a public-domain NASA
portrait (see tests/fixtures/README.md); no other face imagery is used.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.core.settings import get_settings
from app.vision.detection.model_registry import YOLOV11N_FACE, model_path, verify_model_file
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector
from app.vision.sources.interfaces import Frame

pytestmark = pytest.mark.model

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "astronaut_nasa_public_domain.png"


def load_fixture() -> np.ndarray:
    img = cv2.imread(str(FIXTURE))
    assert img is not None, "missing test fixture image"
    return np.asarray(img)


@pytest.fixture(scope="module")
def model_file() -> Path:
    path = model_path(get_settings().models_path, YOLOV11N_FACE)
    verify_model_file(path, YOLOV11N_FACE)  # raises with an actionable message if absent
    return path


@pytest.fixture(scope="module")
def detector(model_file: Path) -> YoloOnnxFaceDetector:
    return YoloOnnxFaceDetector(model_file, conf_threshold=0.5)


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def test_finds_the_face_in_a_real_photograph(detector: YoloOnnxFaceDetector) -> None:
    img = load_fixture()
    faces = detector.detect(Frame(0, 0, img))
    assert len(faces) == 1
    f = faces[0]
    assert f.confidence > 0.7
    # The face occupies roughly the upper-middle of the portrait (hand-checked against the image).
    assert 150 < f.x1 < 215 and 30 < f.y1 < 90 and 240 < f.x2 < 300 and 150 < f.y2 < 210
    assert 0 <= f.x1 < f.x2 <= img.shape[1] and 0 <= f.y1 < f.y2 <= img.shape[0]


def test_box_survives_resizing_and_letterboxing(detector: YoloOnnxFaceDetector) -> None:
    """The same face inside a wide 16:9 canvas must be reported in canvas coordinates."""
    img = load_fixture()
    canvas = np.full((540, 960, 3), 40, dtype=np.uint8)
    canvas[14:526, 300:812] = img  # 512x512 portrait placed at x=300, y=14
    (face,) = detector.detect(Frame(0, 0, canvas))
    direct = detector.detect(Frame(0, 0, img))[0]
    expected = (direct.x1 + 300, direct.y1 + 14, direct.x2 + 300, direct.y2 + 14)
    assert iou((face.x1, face.y1, face.x2, face.y2), expected) > 0.8


def test_blank_and_noise_frames_have_no_faces(detector: YoloOnnxFaceDetector) -> None:
    assert detector.detect(Frame(0, 0, np.zeros((480, 640, 3), dtype=np.uint8))) == []
    rng = np.random.default_rng(0)
    noise = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    assert detector.detect(Frame(0, 0, noise)) == []


def test_two_faces_are_both_found(detector: YoloOnnxFaceDetector) -> None:
    img = cv2.resize(load_fixture(), (400, 400))
    canvas = np.full((500, 1000, 3), 40, dtype=np.uint8)
    canvas[50:450, 50:450] = img
    canvas[50:450, 520:920] = cv2.flip(img, 1)
    faces = detector.detect(Frame(0, 0, canvas))
    assert len(faces) == 2
    xs = sorted(f.x1 for f in faces)
    assert xs[0] < 450 < xs[1]  # one face in each half


def test_confidence_threshold_filters_real_detections(model_file: Path) -> None:
    img = load_fixture()
    strict = YoloOnnxFaceDetector(model_file, conf_threshold=0.99)
    assert strict.detect(Frame(0, 0, img)) == []
    loose = YoloOnnxFaceDetector(model_file, conf_threshold=0.05)
    assert len(loose.detect(Frame(0, 0, img))) >= 1


def test_inference_is_deterministic_and_reports_latency(detector: YoloOnnxFaceDetector) -> None:
    img = load_fixture()
    a = detector.detect(Frame(0, 0, img))
    b = detector.detect(Frame(1, 40, img))
    assert [(f.x1, f.y1, f.x2, f.y2) for f in a] == [(f.x1, f.y1, f.x2, f.y2) for f in b]
    assert 0 < detector.last_inference_ms < 2000


def test_model_metadata_matches_the_documented_contract(model_file: Path) -> None:
    d = YoloOnnxFaceDetector(model_file)
    assert d.input_size == 640
    assert d.model_bytes == YOLOV11N_FACE.size_bytes


def test_spin_wait_and_thread_settings_do_not_change_detections(model_file: Path) -> None:
    img = load_fixture()
    reference = YoloOnnxFaceDetector(model_file).detect(Frame(0, 0, img))
    tuned = YoloOnnxFaceDetector(model_file, intra_op_threads=2, allow_spinning=False)
    got = tuned.detect(Frame(0, 0, img))
    assert len(got) == len(reference) == 1
    assert (
        iou(
            (got[0].x1, got[0].y1, got[0].x2, got[0].y2),
            (reference[0].x1, reference[0].y1, reference[0].x2, reference[0].y2),
        )
        > 0.99
    )
