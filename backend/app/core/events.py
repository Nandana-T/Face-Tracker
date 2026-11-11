"""Audit records for ``events.log``.

PostgreSQL is the source of truth for visit entry/exit events; ``events.log`` is an auditable
*delivery artifact* of them plus a low-rate narrative of the pipeline (tracks, recognition,
registration, persistence problems). Records are one JSON object per line (see ``JsonFormatter``).

* ``audit()``            - best-effort narrative records; a failure never disturbs processing.
* ``EventsLogWriter``    - strict writer for durable business events: it raises when the record could
                           not be written, so the recorder can retry from its outbox.
Never put embeddings, credentials or image bytes in a record.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from app.core.logging import EVENTS_LOGGER_NAME, StrictFileHandler

EventType = Literal["entry", "exit", "registration", "recognition"]


class EventLogger(Protocol):
    def log_event(self, event_type: EventType, **fields: Any) -> None: ...


class FileEventLogger:
    """Writes structured records to ``events.log`` via the ``katomaran.events`` logger."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(EVENTS_LOGGER_NAME)

    def log_event(self, event_type: EventType, **fields: Any) -> None:
        # Callers must not pass embeddings or other sensitive payloads.
        self._logger.info(event_type, extra={"event_type": event_type, **fields})


def audit(message: str, level: int = logging.INFO, **fields: Any) -> None:
    """Narrative audit record (best effort)."""
    logging.getLogger(EVENTS_LOGGER_NAME).log(level, message, extra=fields)


class EventsLogError(Exception):
    pass


class EventsLogWriter:
    """Strict writer: ``write`` raises ``EventsLogError`` when the line did not reach the file."""

    def __init__(self) -> None:
        self._logger = logging.getLogger(EVENTS_LOGGER_NAME)

    def _handler(self) -> StrictFileHandler:
        for handler in self._logger.handlers:
            if isinstance(handler, StrictFileHandler):
                return handler
        raise EventsLogError("events.log is not configured")

    def write(self, message: str, **fields: Any) -> None:
        handler = self._handler()
        before = handler.failures
        self._logger.info(message, extra=fields)
        handler.flush()
        if handler.failures != before:
            raise EventsLogError("events.log could not be written")
