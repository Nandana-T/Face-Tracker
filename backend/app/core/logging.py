"""Centralised logging configuration.

* console    - human readable
* app.log    - JSON lines, rotating
* events.log - JSON lines, written only through the ``katomaran.events`` logger

Directories are created on demand. Never log embeddings, secrets or raw payloads.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.context import request_id_var, session_id_var

EVENTS_LOGGER_NAME = "katomaran.events"
_MANAGED_ATTR = "_katomaran_managed"

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "component",
    "corr",
    "request_id",
    "session_id",
}


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.session_id = session_id_var.get()
        record.component = record.name
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "message": record.getMessage(),
        }
        for key in ("request_id", "session_id"):
            value = getattr(record, key, None)
            if value:
                payload[key] = value
        extras = {
            k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")
        }
        if extras:
            payload["data"] = extras
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(component)-24s %(message)s%(corr)s")

    def format(self, record: logging.LogRecord) -> str:
        parts = []
        if rid := getattr(record, "request_id", None):
            parts.append(f"request_id={rid}")
        if sid := getattr(record, "session_id", None):
            parts.append(f"session_id={sid}")
        record.corr = f" [{' '.join(parts)}]" if parts else ""
        return super().format(record)


def _file_handler(path: Path) -> logging.Handler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    handler.addFilter(CorrelationFilter())
    return handler


def _console_handler() -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setFormatter(ConsoleFormatter())
    handler.addFilter(CorrelationFilter())
    return handler


def _mark(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _MANAGED_ATTR, True)
    return handler


def _reset(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _MANAGED_ATTR, False):
            logger.removeHandler(handler)
            handler.close()


def setup_logging(level: str, log_dir: Path, events_file: Path) -> None:
    """Idempotently configure application and events logging. Safe to call repeatedly."""
    root = logging.getLogger()
    events = logging.getLogger(EVENTS_LOGGER_NAME)
    _reset(root)
    _reset(events)

    root.setLevel(level)
    root.addHandler(_mark(_console_handler()))
    root.addHandler(_mark(_file_handler(log_dir / "app.log")))

    # events.log is mandatory: create it eagerly so its existence never depends on traffic.
    events_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.touch(exist_ok=True)
    events.setLevel(logging.INFO)
    events.propagate = False  # event records go to events.log + console, not app.log
    events.addHandler(_mark(_file_handler(events_file)))
    events.addHandler(_mark(_console_handler()))

    # Uvicorn's access log is replaced by RequestIdMiddleware's request log (with request IDs).
    logging.getLogger("uvicorn.access").disabled = True
