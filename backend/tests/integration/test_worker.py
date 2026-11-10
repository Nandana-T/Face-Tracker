from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from app.workers import video_worker
from tests.conftest import make_settings


def test_worker_exits_nonzero_when_database_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video_worker, "get_settings", lambda: make_settings(tmp_path))
    monkeypatch.setattr(
        video_worker,
        "load_app_config",
        lambda _: __import__("tests.conftest", fromlist=["x"]).make_config(tmp_path),
    )
    assert video_worker.main() == 1


def test_worker_exits_2_on_invalid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"detection": {"frame_skip": -5}}', encoding="utf-8")
    monkeypatch.setattr(
        video_worker,
        "get_settings",
        lambda: make_settings(
            tmp_path, config_file=bad, database_url=SecretStr("postgresql://a@b/c")
        ),
    )
    assert video_worker.main() == 2
