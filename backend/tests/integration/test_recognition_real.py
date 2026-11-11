"""Real recognition models end to end: YOLO -> tracker -> landmarks -> alignment -> SFace -> identity.

These run the actual ONNX models (detector, YuNet landmarks, SFace embedder) on the public-domain
NASA portrait fixture (and a synthetic video made from it) and then push the resulting observations
through the real IdentityManager on PostgreSQL. They fail loudly - never skip - when the models are
missing (scripts\\download-model.ps1 -Recognition). One portrait means ONE identity: what is verified
here is the pipeline wiring, preprocessing correctness and the register-once / recognise-later
behaviour, NOT how well SFace separates different people (see the LFW evaluation for that).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import RecognitionConfig
from app.core.settings import get_settings
from app.db.base import Base
from app.models import ProcessingSession, Visitor
from app.services.identity_manager import IdentityManager, Observation
from app.services.image_store import LocalImageStore
from app.vision.detection.model_registry import YOLOV11N_FACE, model_path
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector
from app.vision.recognition import registry as R
from app.vision.recognition.alignment import align_face
from app.vision.recognition.embedder import EmbeddingError, OrtEmbedder
from app.vision.recognition.service import (
    FaceRecognizer,
    RecognitionRunner,
    RecognitionScheduler,
    make_job,
)
from app.vision.sample_video import build_sample_video
from app.vision.sources.file_source import FileVideoSource
from app.vision.sources.interfaces import Frame
from app.vision.tracking.tracker import FaceByteTracker
from app.workers.pipeline import PipelineSettings, ProcessingPipeline

pytestmark = [pytest.mark.model, pytest.mark.integration]
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "astronaut_nasa_public_domain.png"


@pytest.fixture(scope="module")
def models() -> Path:
    path = get_settings().models_path
    ok, detail = R.recognition_status(path)
    assert ok, detail  # fail, never skip
    return path


@pytest.fixture(scope="module")
def portrait() -> np.ndarray:
    img = cv2.imread(str(FIXTURE))
    assert img is not None
    return np.asarray(img)


def test_models_match_their_pinned_checksums(models: Path) -> None:
    for spec in (R.SFACE, R.YUNET):
        R.verify_model_file(R.model_path(models, spec), spec)


def test_alignment_and_embedding_match_opencv_reference_implementation(
    models: Path, portrait: np.ndarray
) -> None:
    """Our alignment + RGB/0-255 preprocessing reproduce OpenCV's own FaceRecognizerSF exactly."""
    yunet_path, sface_path = R.model_path(models, R.YUNET), R.model_path(models, R.SFACE)
    h, w = portrait.shape[:2]
    detector = cv2.FaceDetectorYN.create(str(yunet_path), "", (w, h), 0.6)
    _, faces = detector.detect(portrait)
    assert faces is not None and len(faces) == 1
    row = faces[0]
    reference = cv2.FaceRecognizerSF.create(str(sface_path), "")
    ref_aligned = reference.alignCrop(portrait, row)
    ref_vec = reference.feature(ref_aligned).reshape(-1)
    ref_vec = ref_vec / np.linalg.norm(ref_vec)

    ours_aligned = align_face(portrait, row[4:14].reshape(5, 2))
    assert np.abs(ours_aligned.astype(float) - ref_aligned.astype(float)).mean() < 0.05
    embedder = OrtEmbedder(sface_path, R.SFACE)
    vec = embedder.embed(ours_aligned)
    assert vec.shape == (128,) and vec.dtype == np.float32
    assert abs(float(np.linalg.norm(vec)) - 1.0) < 1e-5
    assert float(vec @ ref_vec) > 0.999
    # The BGR/RGB convention matters: a BGR-fed model would NOT match the reference.
    swapped = embedder.embed(np.ascontiguousarray(ours_aligned[:, :, ::-1]))
    assert float(swapped @ ref_vec) < 0.99


def test_same_face_under_transformations_stays_similar_and_noise_is_rejected(
    models: Path, portrait: np.ndarray
) -> None:
    detector = YoloOnnxFaceDetector(model_path(models, YOLOV11N_FACE), allow_spinning=False)
    recognizer = FaceRecognizer(models, RecognitionConfig())

    def embed(img: np.ndarray) -> np.ndarray | None:
        faces = detector.detect(Frame(0, 0, img))
        if not faces:
            return None
        f = faces[0]
        job = make_job(img, (f.x1, f.y1, f.x2, f.y2), 1, 1, f.confidence, 0, 0, 0.6)
        return recognizer.process(job).embedding

    base = embed(portrait)
    assert base is not None
    variants = {
        "darker": cv2.convertScaleAbs(portrait, alpha=0.8, beta=-10),
        "smaller": cv2.resize(portrait, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA),
        "blurred_slightly": cv2.GaussianBlur(portrait, (5, 5), 0),
    }
    for name, img in variants.items():
        vec = embed(img)
        assert vec is not None, name
        assert float(vec @ base) > 0.85, name  # same person => high similarity
    noise = np.random.default_rng(0).integers(0, 255, size=portrait.shape, dtype=np.uint8)
    assert embed(noise) is None  # no face, no embedding


def test_quality_gate_rejects_unsuitable_faces_without_embedding(
    models: Path, portrait: np.ndarray
) -> None:
    recognizer = FaceRecognizer(models, RecognitionConfig(min_face_px=40))
    h, w = portrait.shape[:2]
    # a tiny, heavily downscaled face
    tiny = cv2.resize(portrait, None, fx=0.07, fy=0.07, interpolation=cv2.INTER_AREA)
    th, tw = tiny.shape[:2]
    result = recognizer.process(
        make_job(tiny, (0.0, 0.0, tw * 0.3, th * 0.3), 1, 1, 0.9, 0, 0, 0.6)
    )
    assert result.embedding is None and result.crop_jpeg is None
    assert "too_small" in result.reasons
    # a blurred face
    blurred = cv2.GaussianBlur(portrait, (0, 0), 12)
    result = recognizer.process(
        make_job(blurred, (0.0, 0.0, float(w), float(h)), 1, 1, 0.9, 0, 0, 0.6)
    )
    assert result.embedding is None
    assert "cut_by_frame_edge" in result.reasons


def test_embedder_validates_its_inputs_and_outputs(models: Path) -> None:
    embedder = OrtEmbedder(R.model_path(models, R.SFACE), R.SFACE)
    with pytest.raises(EmbeddingError):
        embedder.embed(np.zeros((64, 64, 3), dtype=np.uint8))  # wrong size
    with pytest.raises(EmbeddingError):
        embedder.embed(np.zeros((112, 112, 3), dtype=np.float32))  # wrong dtype
    with pytest.raises(EmbeddingError):
        OrtEmbedder(models / "nope.onnx", R.SFACE)  # not found: a clear error, not a crash


# --------------------------------------------------------------------------- the whole pipeline
@pytest.fixture
def rig(
    isolated_schema: tuple[Any, Engine], tmp_path: Path
) -> tuple[IdentityManager, sessionmaker[Any], Path]:
    _, engine = isolated_schema
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    cfg = RecognitionConfig(faces_dir=str(tmp_path / "faces"))
    mgr = IdentityManager(
        factory, LocalImageStore(tmp_path / "faces"), cfg, R.SFACE.key, R.SFACE.embedding_dim
    )
    mgr.load()
    return mgr, factory, tmp_path


def run_video(
    models: Path, video: Path, session_id: str, mgr: IdentityManager, factory: Any
) -> list[str]:
    """Real detector + tracker + recognition thread; observations go to the real IdentityManager."""
    cfg = mgr.cfg
    with factory() as db:
        if db.get(ProcessingSession, session_id) is None:
            db.add(
                ProcessingSession(
                    id=session_id,
                    source_id="x",
                    source_label="X",
                    source_type="file",
                    state="COMPLETED",
                    frames_read=0,
                    frames_processed=0,
                    frames_skipped=0,
                    frames_dropped=0,
                    corrupt_frames=0,
                    detections_total=0,
                    max_faces_in_frame=0,
                )
            )
            db.commit()
    events: list[str] = []

    def on_result(r: Any) -> None:
        out = mgr.observe(
            Observation(
                session_id,
                r.track_id,
                r.seq,
                R.SFACE.key,
                r.embedding,
                r.quality,
                r.reasons,
                r.timestamp_ms,
                r.frame_id,
                r.crop_jpeg,
            )
        )
        events.extend(e.kind for e in out)

    recognizer = FaceRecognizer(models, cfg)
    runner = RecognitionRunner(recognizer, on_result, RecognitionScheduler(cfg), cfg.queue_size)
    detector = YoloOnnxFaceDetector(model_path(models, YOLOV11N_FACE), allow_spinning=False)
    pipe = ProcessingPipeline(
        FileVideoSource(video),
        detector,
        PipelineSettings(frame_skip=2, realtime_pacing=False, stream_max_fps=1000),
        on_result=lambda r, j: None,
        tracker=FaceByteTracker(),
        recognition=runner,
        recognition_pad=cfg.landmark_pad,
    )
    assert pipe.run().state == "COMPLETED"
    mgr.end_session(session_id)
    return events


def test_real_pipeline_registers_a_face_once_and_recognises_it_later(
    models: Path, rig: tuple[IdentityManager, Any, Path], tmp_path: Path
) -> None:
    mgr, factory, _ = rig
    video = tmp_path / "portrait.mp4"
    build_sample_video(video, FIXTURE, seconds=6.0)  # one portrait, panned and zoomed

    first = run_video(models, video, "a" * 32, mgr, factory)
    assert first.count("registered") == 1, first
    with factory() as db:
        assert db.execute(select(func.count()).select_from(Visitor)).scalar_one() == 1
        visitor = db.execute(select(Visitor)).scalar_one()
        assert visitor.image_path and (tmp_path / "faces" / visitor.image_path).is_file()

    # a second processing session of the same face: recognised, not registered again
    second = run_video(models, video, "b" * 32, mgr, factory)
    assert "recognized" in second and "registered" not in second, second
    # a process restart: the index is rebuilt from PostgreSQL and the face is still known
    restarted = IdentityManager(factory, mgr._store, mgr.cfg, R.SFACE.key, R.SFACE.embedding_dim)  # noqa: SLF001
    restarted.load()
    third = run_video(models, video, "c" * 32, restarted, factory)
    assert "recognized" in third and "registered" not in third, third
    with factory() as db:
        assert db.execute(select(func.count()).select_from(Visitor)).scalar_one() == 1
    # embeddings are never exposed: the runtime stats carry counters only
    assert "embedding" not in str(restarted.runtime_stats()).lower().replace(
        "indexed_embeddings", ""
    )
