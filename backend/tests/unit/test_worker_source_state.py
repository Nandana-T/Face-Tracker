"""The worker must forward RTSP connection state (reconnecting / connected) to the API.

Regression: the callback was never wired, so a lost camera stream looked "connected" forever in the
session and in the UI. Found by the local RTSP integration test (scripts/verify_rtsp_local.py).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.config import AppConfig
from app.vision.sources.interfaces import SourceOpenError
from app.workers import protocol as P
from app.workers.agent import ActiveSession, Outbox, WorkerAgent
from tests.conftest import make_settings


def test_source_state_callback_reaches_the_api_as_session_state(tmp_path: Any) -> None:
    captured: dict[str, Any] = {}

    def builder(config: Any, source_id: str, **kw: Any) -> Any:
        captured.update(kw)
        raise SourceOpenError("no camera in this test", "camera_unavailable")  # stop after wiring

    loop = asyncio.new_event_loop()
    try:
        agent = WorkerAgent(make_settings(tmp_path), AppConfig(), source_builder=builder)
        agent._outbox = Outbox(loop)
        agent._session_thread(ActiveSession("a" * 32), "rtsp-source")
        on_state = captured["on_state"]
        on_state("reconnecting")
        on_state("connected")
        control, _frame, _obs = agent._outbox.drain()
    finally:
        loop.close()
    states = [json.loads(m) for m in control if json.loads(m)["type"] == P.MSG_STATE]
    assert [(m["state"], m["session_id"]) for m in states] == [
        ("reconnecting", "a" * 32),
        ("connected", "a" * 32),
    ]
