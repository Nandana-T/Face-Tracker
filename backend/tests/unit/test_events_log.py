"""events.log writers: best-effort narrative records and the strict writer used for durable events."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.events import EventsLogError, EventsLogWriter, audit
from app.core.logging import StrictFileHandler, _reset, setup_logging


@pytest.fixture
def log_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "logs" / "events.log"
    setup_logging("WARNING", tmp_path / "logs", path)
    yield path
    _reset(logging.getLogger())
    _reset(logging.getLogger("katomaran.events"))


def lines(path: Path) -> list[dict[str, object]]:
    for h in logging.getLogger("katomaran.events").handlers:
        h.flush()
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def test_events_log_exists_eagerly_and_audit_records_are_structured_json(log_path: Path) -> None:
    assert log_path.is_file()  # mandatory file: its existence never depends on traffic
    audit("track.created", session_id="s1", track_id=3)
    audit("persistence.failure", logging.ERROR, visit_id="v", reason="queue_overflow")
    rec = lines(log_path)
    assert [r["message"] for r in rec] == ["track.created", "persistence.failure"]
    assert rec[0]["session_id"] == "s1"  # the explicit session of the record is kept
    assert rec[0]["data"] == {"track_id": 3}
    assert rec[1]["level"] == "ERROR" and "timestamp" in rec[1]


def test_strict_writer_delivers_the_line(log_path: Path) -> None:
    EventsLogWriter().write("visit.entry", event_id="e1", delivery_attempt=1)
    assert lines(log_path)[0]["data"] == {"event_id": "e1", "delivery_attempt": 1}


def test_strict_writer_reports_a_failed_write_instead_of_swallowing_it(
    log_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = next(
        h
        for h in logging.getLogger("katomaran.events").handlers
        if isinstance(h, StrictFileHandler)
    )

    def broken(_data: str) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(handler.stream, "write", broken)
    with pytest.raises(EventsLogError):
        EventsLogWriter().write("visit.entry", event_id="e2")
    assert handler.failures == 1
    # best-effort narrative records never raise, whatever happens
    audit("track.created", session_id="s1", track_id=1)


def test_strict_writer_without_a_configured_log_is_an_error(tmp_path: Path) -> None:
    _reset(logging.getLogger("katomaran.events"))
    with pytest.raises(EventsLogError):
        EventsLogWriter().write("visit.entry", event_id="e3")
