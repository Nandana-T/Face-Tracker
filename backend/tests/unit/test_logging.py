from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.context import request_id_var
from app.core.events import FileEventLogger
from app.core.logging import setup_logging


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    yield
    # Detach handlers pointing into tmp dirs so later tests/teardown can delete them.
    for name in ("", "katomaran.events"):
        logger = logging.getLogger(name)
        for h in list(logger.handlers):
            if getattr(h, "_katomaran_managed", False):
                logger.removeHandler(h)
                h.close()
    logging.getLogger("katomaran.events").propagate = True


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def test_creates_missing_directories_and_events_log(tmp_path: Path) -> None:
    log_dir = tmp_path / "does" / "not" / "exist"
    events = tmp_path / "other" / "events.log"
    setup_logging("INFO", log_dir, events)
    assert events.is_file()
    logging.getLogger("t").info("hello")
    assert (log_dir / "app.log").is_file()


def test_app_log_is_structured_with_correlation(tmp_path: Path) -> None:
    setup_logging("INFO", tmp_path, tmp_path / "events.log")
    token = request_id_var.set("req-12345678")
    try:
        logging.getLogger("katomaran.test").warning("something", extra={"k": 1})
    finally:
        request_id_var.reset(token)
    for h in logging.getLogger().handlers:
        h.flush()
    record = lines(tmp_path / "app.log")[-1]
    assert record["level"] == "WARNING"
    assert record["component"] == "katomaran.test"
    assert record["request_id"] == "req-12345678"
    assert record["data"] == {"k": 1}
    assert "timestamp" in record


def test_exception_details_logged(tmp_path: Path) -> None:
    setup_logging("INFO", tmp_path, tmp_path / "events.log")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("x").exception("failed")
    record = lines(tmp_path / "app.log")[-1]
    assert "RuntimeError: boom" in str(record["exception"])


def test_events_go_only_to_events_log_and_setup_is_idempotent(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    setup_logging("INFO", tmp_path, events)
    setup_logging("INFO", tmp_path, events)
    FileEventLogger().log_event("entry", visit_ref="v-test")
    app_text = (tmp_path / "app.log").read_text(encoding="utf-8")
    recs = lines(events)
    assert len(recs) == 1  # no duplicated handlers
    assert recs[0]["message"] == "entry"
    assert "entry" not in app_text


def test_no_events_emitted_by_setup(tmp_path: Path) -> None:
    events = tmp_path / "events.log"
    setup_logging("INFO", tmp_path, events)
    assert events.read_text(encoding="utf-8") == ""
