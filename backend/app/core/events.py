"""Event logger interface for future visitor entry/exit tracking.

Phase 0 defines the contract and a file-backed implementation. Nothing in Phase 0 emits
events - fabricating entry/exit records is explicitly out of scope.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, Protocol

from app.core.logging import EVENTS_LOGGER_NAME

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
