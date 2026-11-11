"""Pure recognition building blocks (no model files): alignment, quality gate, index, scheduling,
image store, observation parsing, wire format."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from app.core.config import RecognitionConfig
from app.services.identity_manager import Observation, parse_observation, visitor_label
from app.services.image_store import ImageStoreError, LocalImageStore, validate_jpeg
from app.vision.recognition.alignment import (
    ALIGNED_SIZE,
    TEMPLATE,
    AlignmentError,
    align_face,
    similarity_transform,
)
from app.vision.recognition.embedder import EmbeddingError, normalize, validate_embedding
from app.vision.recognition.matching import EmbeddingIndex
from app.vision.recognition.quality import (
    QualityConfig,
    assess,
    pose_metrics,
    sharpness,
)
from app.vision.recognition.service import RecognitionScheduler, make_job
from app.workers import protocol as P

GOOD = TEMPLATE.copy()  # landmarks already in template position: frontal, upright
FRAME = (1280, 720)


def jpeg_bytes(size: int = 64) -> bytes:
    img = np.random.default_rng(0).integers(0, 255, size=(size, size, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


# ------------------------------------------------------------------------------------ alignment
def test_alignment_maps_landmarks_onto_the_template() -> None:
    img = np.zeros((300, 300, 3), dtype=np.uint8)
    scale, shift = 2.0, np.array([40.0, 25.0], dtype=np.float32)
    pts = TEMPLATE * scale + shift  # the same face, twice as large, somewhere else
    matrix = similarity_transform(pts)
    mapped = pts @ matrix[:, :2].T + matrix[:, 2]
    assert np.abs(mapped - TEMPLATE).max() < 0.05
    assert align_face(img, pts).shape == (ALIGNED_SIZE, ALIGNED_SIZE, 3)


def test_alignment_compensates_for_head_roll() -> None:
    angle = np.deg2rad(20)
    rot = np.array(
        [[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], dtype=np.float32
    )
    pts = (TEMPLATE - 56) @ rot.T + 150
    mapped = pts @ similarity_transform(pts)[:, :2].T + similarity_transform(pts)[:, 2]
    assert np.abs(mapped - TEMPLATE).max() < 0.1


@pytest.mark.parametrize(
    "pts",
    [
        np.full((5, 2), np.nan, dtype=np.float32),
        np.zeros((4, 2), dtype=np.float32),
        np.zeros((5, 2), dtype=np.float32),  # all landmarks on one point: degenerate
    ],
)
def test_alignment_rejects_bad_landmarks(pts: np.ndarray) -> None:
    with pytest.raises(AlignmentError):
        align_face(np.zeros((100, 100, 3), dtype=np.uint8), pts)


def test_alignment_rejects_empty_or_grey_images() -> None:
    with pytest.raises(AlignmentError):
        align_face(np.zeros((0, 0, 3), dtype=np.uint8), GOOD)
    with pytest.raises(AlignmentError):
        align_face(np.zeros((100, 100), dtype=np.uint8), GOOD)


# ------------------------------------------------------------------------------------ quality
def sharp_crop() -> np.ndarray:
    return np.random.default_rng(1).integers(0, 255, size=(112, 112, 3), dtype=np.uint8)


def assess_default(**over: object) -> tuple[bool, tuple[str, ...]]:
    kw: dict[str, Any] = {
        "box": (400.0, 200.0, 520.0, 330.0),
        "detection_confidence": 0.9,
        "frame_size": FRAME,
        "landmarks": GOOD,
        "aligned_bgr": sharp_crop(),
    }
    kw.update(over)
    r = assess(QualityConfig(), **kw)
    return r.ok, r.reasons


def test_a_good_face_passes_the_quality_gate() -> None:
    ok, reasons = assess_default()
    assert ok and reasons == ()


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"box": (400.0, 200.0, 425.0, 225.0)}, "too_small"),
        ({"detection_confidence": 0.5}, "low_detection_confidence"),
        ({"box": (0.0, 200.0, 120.0, 330.0)}, "cut_by_frame_edge"),
        ({"box": (1200.0, 200.0, 1280.0, 330.0)}, "cut_by_frame_edge"),
        ({"landmarks": None}, "no_landmarks"),
        ({"aligned_bgr": np.full((112, 112, 3), 120, dtype=np.uint8)}, "blurry"),
    ],
)
def test_unsuitable_faces_are_rejected_with_a_machine_readable_reason(
    override: dict[str, object], reason: str
) -> None:
    ok, reasons = assess_default(**override)
    assert not ok and reason in reasons


def test_pose_checks_reject_profile_tilted_and_looking_down_faces() -> None:
    profile = GOOD.copy()
    profile[2, 0] += 22  # nose far to one side of the eyes
    assert "turned_away" in assess_default(landmarks=profile)[1]
    tilted = (GOOD - 56) @ np.array(
        [[0.7, -0.7], [0.7, 0.7]], dtype=np.float32
    ).T + 56  # ~45 degrees
    assert "tilted" in assess_default(landmarks=tilted)[1]
    down = GOOD.copy()
    down[2, 1] = down[0, 1] + 2  # nose almost level with the eyes
    assert "looking_up_or_down" in assess_default(landmarks=down)[1]
    yaw, roll, pitch = pose_metrics(GOOD)
    assert yaw < 0.05 and roll < 1 and 0.8 < pitch < 1.2


def test_sharpness_orders_sharp_above_blurred() -> None:
    sharp = sharp_crop()
    assert sharpness(cv2.GaussianBlur(sharp, (9, 9), 0)) < sharpness(sharp) / 5


# ------------------------------------------------------------------------------------ embedding validation
def test_normalisation_and_validation() -> None:
    v = normalize(np.array([3.0, 4.0]))
    assert np.allclose(v, [0.6, 0.8]) and abs(float(np.linalg.norm(v)) - 1) < 1e-6
    assert validate_embedding(np.ones(128), 128).shape == (128,)
    for bad in (np.zeros(8), np.array([np.nan, 1.0]), np.array([np.inf, 0.0])):
        with pytest.raises(EmbeddingError):
            normalize(bad)
    with pytest.raises(EmbeddingError):
        validate_embedding(np.ones(64), 128)


# ------------------------------------------------------------------------------------ index
def vec(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_index_returns_best_and_best_other_visitor_and_the_margin() -> None:
    idx = EmbeddingIndex()
    idx.add("m", "A", vec(1, 0, 0), "e1")
    idx.add("m", "A", vec(0.9, 0.1, 0), "e2")  # a second embedding of the same visitor
    idx.add("m", "B", vec(0, 1, 0), "e3")
    r = idx.search("m", vec(1, 0.05, 0))
    assert r.best is not None and r.best.visitor_id == "A" and r.second is not None
    assert r.second.visitor_id == "B"  # not A's second embedding
    assert r.margin > 0.9


def test_index_partitions_by_model_version_and_is_idempotent() -> None:
    idx = EmbeddingIndex()
    assert idx.add("m1", "A", vec(1, 0), "e1") is True
    assert idx.add("m1", "A", vec(1, 0), "e1") is False  # same embedding id: not added twice
    assert idx.search("m2", vec(1, 0)).best is None  # another model's vectors are never compared
    assert len(idx) == 1 and idx.visitor_count("m1") == 1
    with pytest.raises(ValueError):
        idx.add("m1", "B", vec(1, 0, 0), "e2")  # dimension mismatch inside a partition


def test_index_removal_and_empty_search() -> None:
    idx = EmbeddingIndex()
    assert idx.search("m", vec(1, 0)).best is None
    idx.add("m", "A", vec(1, 0), "e1")
    idx.add("m", "B", vec(0, 1), "e2")
    assert idx.remove_visitor("A") == 1
    r = idx.search("m", vec(1, 0))
    assert r.best is not None and r.best.visitor_id == "B"
    idx.remove_visitor("B")
    assert idx.search("m", vec(1, 0)).best is None


# ------------------------------------------------------------------------------------ scheduling
def test_scheduler_warms_up_quickly_then_refreshes_rarely() -> None:
    cfg = RecognitionConfig(
        observation_interval_seconds=0.5, warmup_observations=3, refresh_interval_seconds=5
    )
    s = RecognitionScheduler(cfg)
    sent: list[int] = []
    for t in range(0, 12_000, 120):
        if s.select([7], t):
            sent.append(t)
    assert len(sent) <= 3 + 2 and sent[:3] == [0, 600, 1200]  # three quick ones, then every ~5 s
    assert sent[3] >= 1200 + 5000 - 120


def test_scheduler_respects_the_time_budget_and_serves_the_longest_waiting_first() -> None:
    cfg = RecognitionConfig(budget_ms_per_frame=80)
    s = RecognitionScheduler(cfg)
    s.ema_ms = 40.0  # budget for two jobs per frame
    first = s.select([1, 2, 3, 4, 5], 0)
    assert len(first) == 2
    second = s.select([1, 2, 3, 4, 5], 600)
    served = {t for t, _ in first} | {t for t, _ in second}
    assert len(served) == 4  # fairness: nobody is served twice before the others
    assert all(seq == 1 for _, seq in first + second)


def test_sequence_numbers_stay_monotonic_when_a_track_briefly_disappears() -> None:
    """A track that is not observed for a few frames must keep counting: restarting at 1 would make
    the API drop the repeats as duplicate observation ids (found on the real footage)."""
    s = RecognitionScheduler(RecognitionConfig(observation_interval_seconds=0.5))
    assert s.select([7], 0) == [(7, 1)]
    assert s.select([], 120) == []  # the track is LOST / not detected in this frame
    assert s.select([7], 700) == [(7, 2)]
    assert sorted(s.select([7, 9], 1300)) == [(7, 3), (9, 1)]  # never-served tracks go first


def test_job_crop_is_a_bounded_copy_not_the_frame() -> None:
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    job = make_job(frame, (900.0, 500.0, 980.0, 600.0), 3, 1, 0.9, 1000, 25, 0.6)
    assert job.crop.shape[0] < 250 and job.crop.shape[1] < 250  # box + 60% padding, never the frame
    assert not np.shares_memory(job.crop, frame)
    bx1, by1, bx2, by2 = job.box_in_crop
    assert 0 <= bx1 < bx2 <= job.crop.shape[1] and 0 <= by1 < by2 <= job.crop.shape[0]
    edge = make_job(
        frame, (0.0, 0.0, 50.0, 60.0), 3, 2, 0.9, 0, 0, 0.6
    )  # clipped at the frame corner
    assert edge.box_in_crop[0] == 0 and edge.crop.shape[0] > 0


# ------------------------------------------------------------------------------------ image store
def test_image_store_is_content_addressed_atomic_and_confined(tmp_path: Path) -> None:
    store = LocalImageStore(tmp_path / "faces")
    vid = "ab" * 16
    data = jpeg_bytes()
    path, digest = store.save(vid, data)
    assert path.startswith(vid + "/") and len(digest) == 64
    assert store.save(vid, data) == (path, digest)  # the same crop is never stored twice
    assert len(store.all_files()) == 1 and store.read(path) == data
    assert not list((tmp_path / "faces").rglob("*.part"))
    for bad in (
        "../x.jpg",
        "..\\x.jpg",
        f"{vid}/../../x.jpg",
        "C:/Windows/win.ini",
        "/etc/passwd",
        f"{vid}/x",
    ):
        with pytest.raises(ImageStoreError):
            store.read(bad)
        assert store.exists(bad) is False
    with pytest.raises(ImageStoreError):
        store.save("../evil", data)  # visitor ids are validated too


@pytest.mark.parametrize(
    "payload", [b"", b"not an image", b"\xff\xd8garbage", b"\xff\xd8" + b"0" * 600_000]
)
def test_image_store_rejects_untrusted_payloads(tmp_path: Path, payload: bytes) -> None:
    with pytest.raises(ImageStoreError):
        LocalImageStore(tmp_path / "f").save("ab" * 16, payload)
    with pytest.raises(ImageStoreError):
        validate_jpeg(payload)


def test_image_store_erasure(tmp_path: Path) -> None:
    store = LocalImageStore(tmp_path / "faces")
    store.save("ab" * 16, jpeg_bytes())
    store.save("cd" * 16, jpeg_bytes(48))
    assert store.delete_visitor("ab" * 16) == 1
    assert store.delete_visitor("ab" * 16) == 0
    assert len(store.all_files()) == 1


# ------------------------------------------------------------------------------------ observation wire format
def header(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": P.KIND_OBSERVATION,
        "session_id": "s" * 32,
        "track_id": 4,
        "seq": 2,
        "model_version": "sface-2021dec",
        "dim": 128,
        "quality": 0.7,
        "reasons": [],
        "embedding_b64": base64.b64encode(np.ones(128, dtype="<f4").tobytes()).decode(),
        "timestamp_ms": 1200,
        "frame_id": 30,
    }
    base.update(over)
    return base


def test_valid_observation_is_parsed() -> None:
    obs = parse_observation(header(), jpeg_bytes(), 128)
    assert isinstance(obs, Observation) and obs.track_id == 4 and obs.obs_id == f"{'s' * 32}:4:2"
    assert obs.embedding is not None and obs.embedding.shape == (128,) and obs.crop_jpeg
    unusable = parse_observation(header(embedding_b64=None, reasons=["blurry"]), b"", 128)
    assert (
        unusable.embedding is None
        and unusable.reasons == ("blurry",)
        and unusable.crop_jpeg is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"track_id": 0},
        {"track_id": "x"},
        {"seq": -1},
        {"quality": 1.5},
        {"quality": -0.1},
        {"embedding_b64": "!!not base64!!"},
        {"embedding_b64": base64.b64encode(np.ones(64, dtype="<f4").tobytes()).decode()},
        {"session_id": None},
        {"model_version": None},
    ],
)
def test_malformed_observations_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        parse_observation(header(**bad), b"", 128)


def test_missing_fields_are_rejected() -> None:
    h = header()
    del h["track_id"]
    with pytest.raises(ValueError):
        parse_observation(h, b"", 128)


def test_protocol_allows_an_observation_without_crop_but_not_a_bogus_payload() -> None:
    packed = P.pack_frame(header(), b"")
    h, payload = P.unpack_frame(packed, require_jpeg=False)
    assert h["kind"] == P.KIND_OBSERVATION and payload == b""
    with pytest.raises(P.ProtocolError):
        P.unpack_frame(packed)  # a video frame must carry a JPEG
    with pytest.raises(P.ProtocolError):
        P.unpack_frame(P.pack_frame(header(), b"not a jpeg"), require_jpeg=False)


def test_visitor_label_format() -> None:
    assert visitor_label(124) == "V00124" and visitor_label(1) == "V00001"


def test_recognition_config_validation() -> None:
    with pytest.raises(ValueError):
        RecognitionConfig(similarity_threshold=0.3, novelty_threshold=0.4)
    for bad in (
        {"max_embeddings_per_visitor": 0},
        {"queue_size": 0},
        {"retention_days": -1},
        {"unknown": 1},
    ):
        with pytest.raises(ValueError):
            RecognitionConfig(**bad)
