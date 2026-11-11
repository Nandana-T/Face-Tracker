"""VisitService: bounded queue, ordered persistence, retry with back-off, dead letters, live notifications."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import VisitsConfig
from app.core.logging import _reset, setup_logging
from app.models import VisitEvent
from app.services.event_recorder import EventRecorder
from app.services.evidence_store import EvidenceStore
from app.services.visit_manager import Crop, VisitAction, VisitSnapshot, visit_id_for
from app.services.visit_service import VisitService

SID, VISITOR = "s" * 32, "a" * 32
T0 = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)


def pic() -> bytes:
    img = np.random.default_rng(3).integers(30, 230, size=(96, 96, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return bytes(buf.tobytes())


def snap(ordinal: int = 1, **kw: Any) -> VisitSnapshot:
    key = f"{SID}:{VISITOR}:{ordinal}"
    base: dict[str, Any] = {
        "id": visit_id_for(key), "visit_key": key, "session_id": SID, "source_id": "x",
        "source_label": "X", "visitor_id": VISITOR, "visitor_number": 1, "state": "ACTIVE",
        "entry_video_ms": 0, "entry_wall": T0, "last_video_ms": 9000,
        "last_wall": T0 + timedelta(seconds=9), "track_segments": 1,
    }  # fmt: skip
    return VisitSnapshot(**{**base, **kw})


def entry(ordinal: int = 1) -> VisitAction:
    return VisitAction("entry", snap(ordinal), "entry", Crop(pic(), 0.8, 0), "observation")


def terminal(ordinal: int = 1) -> VisitAction:
    s = snap(ordinal, state="EXIT_PENDING", exit_basis="absence", closure_reason="absence_timeout")
    return VisitAction("terminal", s, "exit", Crop(pic(), 0.8, 9), "last_good_observation")


@pytest.fixture
def svc(tmp_path: Path, sqlite_engine: Engine) -> Any:
    setup_logging("WARNING", tmp_path / "logs", tmp_path / "logs" / "events.log")
    factory = sessionmaker(sqlite_engine, expire_on_commit=False)
    cfg = VisitsConfig(
        max_attempts=3, retry_base_seconds=0.01, retry_max_seconds=0.02, queue_size=10
    )
    service = VisitService(cfg, EventRecorder(factory, EvidenceStore(tmp_path / "ev"), cfg))
    service.notify = lambda sid, msg: service.sent.append((sid, msg))  # type: ignore[attr-defined]
    service.sent = []  # type: ignore[attr-defined]
    service.factory = factory  # type: ignore[attr-defined]
    yield service
    _reset(logging.getLogger())
    _reset(logging.getLogger("katomaran.events"))


def events(svc: Any) -> list[str]:
    with svc.factory() as db:
        return list(
            db.execute(select(VisitEvent.event_type).order_by(VisitEvent.event_time)).scalars()
        )


def test_actions_are_recorded_in_order_and_announced_once(svc: Any) -> None:
    async def go() -> None:
        svc.start()
        svc.manager.begin_session(SID, "x", "X")
        svc.submit([entry(), terminal()])
        assert await svc.drain(5)
        await svc.stop()

    asyncio.run(go())
    assert events(svc) == ["entry", "exit"]
    kinds = [m["event"]["type"] for _, m in svc.sent if m["type"] == "visit_event"]
    assert kinds == ["entry", "exit"]
    assert svc.runtime_stats()["queue_depth"] == 0


def test_a_database_outage_is_retried_and_nothing_is_lost_or_duplicated(
    svc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = svc.recorder.record
    failures = {"n": 2}

    def flaky(action: VisitAction, *a: Any) -> Any:
        if failures["n"] > 0:
            failures["n"] -= 1
            raise ConnectionError("database is down")
        return real(action, *a)

    monkeypatch.setattr(svc.recorder, "record", flaky)

    async def go() -> None:
        svc.start()
        svc.submit([entry()])
        assert await svc.drain(5)
        await svc.stop()

    asyncio.run(go())
    assert events(svc) == ["entry"]
    assert svc.stats["retries"] == 2 and svc.stats["dead_letters"] == 0
    assert len([m for _, m in svc.sent if m["type"] == "visit_event"]) == 1


def test_a_permanently_failing_event_becomes_a_reported_dead_letter(
    svc: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(svc.recorder, "record", lambda *a: (_ for _ in ()).throw(OSError("gone")))

    async def go() -> None:
        svc.start()
        svc.submit([entry(1), entry(2)])
        assert await svc.drain(5)  # a dead letter does not block the queue
        await svc.stop()

    asyncio.run(go())
    assert svc.stats["dead_letters"] == 2 and svc.stats["retries"] == 6
    text = (tmp_path / "logs" / "events.log").read_text(encoding="utf-8")
    assert text.count('"persistence.failure"') == 2 and "max_attempts_exceeded" in text


def test_conflicting_event_is_rejected_and_reported(svc: Any) -> None:
    other = VisitAction(
        "entry",
        snap(entry_wall=T0 + timedelta(hours=1)),
        "entry",
        Crop(pic(), 0.8, 0),
        "observation",
    )

    async def go() -> None:
        svc.start()
        svc.submit([entry(), other])
        assert await svc.drain(5)
        await svc.stop()

    asyncio.run(go())
    assert events(svc) == ["entry"] and svc.stats["rejected"] == 1


def test_a_full_queue_is_reported_never_silently_ignored(svc: Any, tmp_path: Path) -> None:
    async def go() -> None:
        # consumer not started: the queue (size 10) fills up
        svc.submit([entry(i) for i in range(1, 14)])

    asyncio.run(go())
    assert svc.stats["queue_overflow"] == 3
    assert (tmp_path / "logs" / "events.log").read_text(encoding="utf-8").count(
        "queue_overflow"
    ) == 3


def test_state_changes_are_announced_without_touching_the_database(svc: Any) -> None:
    async def go() -> None:
        svc.start()
        svc.submit([VisitAction("state", snap(state="TEMPORARILY_MISSING"))])
        await svc.stop()

    asyncio.run(go())
    assert svc.sent[0][1]["state"] == "TEMPORARILY_MISSING" and events(svc) == []
    with svc.factory() as db:
        assert db.execute(select(func.count()).select_from(VisitEvent)).scalar_one() == 0
