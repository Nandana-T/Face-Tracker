from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import AppConfig, ConfigError, load_app_config
from app.core.paths import project_root, resolve_path
from tests.conftest import make_settings


def write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


def test_defaults_when_sections_omitted(tmp_path: Path) -> None:
    config = load_app_config(write(tmp_path, {}))
    assert config.detection.frame_skip == 2
    assert config.recognition.similarity_threshold == 0.55
    assert config.logging.events_file == "logs/events.log"
    assert config.video.source_type == "file"


def test_repository_config_is_valid() -> None:
    config = load_app_config(project_root() / "config" / "config.json")
    assert config.tracking.max_missing_seconds == 2
    assert config.tracking.enabled and config.tracking.confirm_hits == 3


@pytest.mark.parametrize(
    "data",
    [
        {"detection": {"frame_skip": -1}},
        {"detection": {"confidence_threshold": 1.5}},
        {"video": {"source_type": "ftp"}},
        {"tracking": {"max_missing_seconds": 0}},
        {"tracking": {"confirm_hits": 0}},
        {"tracking": {"match_iou": 0}},
        {"tracking": {"high_confidence": 1.2}},
        {"tracking": {"max_tracks": 0}},
        {"tracking": {"unknown_knob": 1}},
        {"logging": {"level": "LOUD"}},
        {"logging": {"events_file": " "}},
        {"unknown_section": {}},
        {"detection": {"frame_skipp": 1}},
    ],
)
def test_invalid_config_rejected_with_clear_message(tmp_path: Path, data: object) -> None:
    with pytest.raises(ConfigError, match="Invalid configuration"):
        load_app_config(write(tmp_path, data))


def test_malformed_json_and_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_app_config(write(tmp_path, "{nope"))
    with pytest.raises(ConfigError, match="not found"):
        load_app_config(tmp_path / "absent.json")
    with pytest.raises(ConfigError, match="JSON object"):
        load_app_config(write(tmp_path, []))


def test_paths_resolve_independent_of_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_path("logs/events.log")
    assert resolved == project_root() / "logs" / "events.log"
    assert AppConfig().events_log_path == resolved


def test_settings_normalise_driver_and_split_origins(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path,
        database_url=SecretStr("postgres://u:p@h/db"),
        cors_origins="https://a.example, https://b.example",
    )
    assert s.database_url.get_secret_value().startswith("postgresql+psycopg://")
    assert s.cors_origins == ["https://a.example", "https://b.example"]


def test_wildcard_cors_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit origins"):
        make_settings(tmp_path, cors_origins=["*"])


def test_database_url_never_in_repr(tmp_path: Path) -> None:
    s = make_settings(tmp_path, database_url=SecretStr("postgresql://u:topsecret@h/db"))
    assert "topsecret" not in repr(s)
