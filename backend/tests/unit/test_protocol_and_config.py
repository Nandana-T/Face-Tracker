from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import AppConfig, ConfigError, load_app_config
from app.workers import protocol as P
from tests.conftest import make_full_config


# ---- wire protocol ---------------------------------------------------------------------------
def test_frame_roundtrip() -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 50
    header, payload = P.unpack_frame(P.pack_frame({"session_id": "a", "frame_id": 3}, jpeg))
    assert header == {"session_id": "a", "frame_id": 3}
    assert payload == jpeg


@pytest.mark.parametrize(
    "message",
    [
        b"",
        b"\x00\x00",
        b"\x00\x00\x00\x09{}",
        b"\x00\x00\x00\x02[]\xff\xd8",
        b"\x00\x00\x00\x02{}nojpeg",
    ],
)
def test_malformed_frames_rejected(message: bytes) -> None:
    with pytest.raises(P.ProtocolError):
        P.unpack_frame(message)


def test_oversized_frame_rejected() -> None:
    with pytest.raises(P.ProtocolError):
        P.pack_frame({}, b"\xff\xd8" + b"0" * P.MAX_FRAME_BYTES)
    with pytest.raises(P.ProtocolError):
        P.unpack_frame(b"\x00" * (P.MAX_FRAME_BYTES + 1))


def test_control_messages_validated() -> None:
    assert P.decode_control(P.encode_control({"type": "hello"}))["type"] == "hello"
    for bad in ["not json", "[]", '{"type": 3}', '{"no": "type"}', " " * (P.MAX_CONTROL_BYTES + 1)]:
        with pytest.raises(P.ProtocolError):
            P.decode_control(bad)


# ---- configuration ---------------------------------------------------------------------------
def test_shipped_config_is_valid() -> None:
    cfg = load_app_config(Path(__file__).resolve().parents[3] / "config" / "config.json")
    assert cfg.detection.model == "yolov11n-face"
    assert cfg.detection.input_size == 640
    assert cfg.detection.frame_skip == 2
    assert AppConfig().stream.max_fps > 0


def test_rtsp_source_must_use_env_reference(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="url_env"):
        make_full_config(
            tmp_path,
            video={"sources": [{"id": "cam", "label": "Cam", "type": "rtsp", "path": "x.mp4"}]},
        )
    cfg = make_full_config(
        tmp_path,
        video={"sources": [{"id": "cam", "label": "Cam", "type": "rtsp", "url_env": "RTSP_CAM"}]},
    )
    assert cfg.video.sources[0].url_env == "RTSP_CAM"
    # A raw URL (with credentials) is not even a valid environment-variable name.
    with pytest.raises(ValueError):
        make_full_config(
            tmp_path,
            video={
                "sources": [{"id": "c", "label": "C", "type": "rtsp", "url_env": "rtsp://u:p@h/x"}]
            },
        )


def test_file_source_requires_path_and_unique_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="path"):
        make_full_config(tmp_path, video={"sources": [{"id": "a", "label": "A", "type": "file"}]})
    dup = {"id": "a", "label": "A", "type": "file", "path": "a.mp4"}
    with pytest.raises(ValueError, match="unique"):
        make_full_config(tmp_path, video={"sources": [dup, dup]})
    with pytest.raises(ValueError):
        make_full_config(tmp_path, video={"sources": [{**dup, "id": "Bad Id!"}]})


def test_incompatible_detection_settings_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown detection model"):
        make_full_config(tmp_path, detection={"model": "nope"})
    with pytest.raises(ValueError, match="fixed input size"):
        make_full_config(tmp_path, detection={"input_size": 320})
    for bad in ({"frame_skip": -1}, {"confidence_threshold": 1.5}, {"iou_threshold": 0}):
        with pytest.raises(ValueError):
            make_full_config(tmp_path, detection=bad)
    with pytest.raises(ValueError):
        make_full_config(tmp_path, stream={"max_fps": 0})


def test_unknown_keys_still_rejected(tmp_path: Path) -> None:
    path = tmp_path / "c.json"
    path.write_text('{"worker": {"queue_sizee": 3}}', encoding="utf-8")
    with pytest.raises(ConfigError, match="worker.queue_sizee"):
        load_app_config(path)


def test_cpu_defaults_are_conservative() -> None:
    """ORT spin-waiting burned ~70% of a 16-thread CPU at real-time rates (see the benchmark)."""
    assert AppConfig().detection.cpu_spin_wait is False
    shipped = load_app_config(Path(__file__).resolve().parents[3] / "config" / "config.json")
    assert shipped.detection.cpu_spin_wait is False
    assert shipped.detection.intra_op_threads == 4
