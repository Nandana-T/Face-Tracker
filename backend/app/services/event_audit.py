"""Consistency check between PostgreSQL (source of truth) and ``events.log`` (delivery artifact).

Finds, for the durable visit events only (records named ``visit.entry|exit|interrupted``):
  * missing_in_log        DB says DELIVERED but no line exists (lost/truncated/rotated away file)
  * logged_not_in_db      a line whose event is not in the database (restored DB, erased visit)
  * redelivered           more than one line, each with a distinct ``delivery_attempt`` - EXPLAINED
                          (a crash between writing the line and acknowledging it)
  * unexplained_duplicates the same event with the same ``delivery_attempt`` twice - a real defect
  * pending_delivery      committed events whose line has not been written yet (retry in progress)
Rotated files (events.log.1 ... .5) are read as well.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models.visit import EventDeliveryOutbox, VisitEvent

DURABLE = ("visit.entry", "visit.exit", "visit.interrupted")


def log_files(path: Path) -> list[Path]:
    files = [path.with_name(f"{path.name}.{i}") for i in range(5, 0, -1)] + [path]
    return [f for f in files if f.is_file()]


def read_durable_records(path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    for file in log_files(path):
        with file.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if isinstance(obj, dict) and obj.get("message") in DURABLE:
                    data = obj.get("data")
                    if isinstance(data, dict) and isinstance(data.get("event_id"), str):
                        records.append(data)
    return records, malformed


def verify_events_log(factory: sessionmaker[Session], log_path: Path) -> dict[str, Any]:
    records, malformed = read_durable_records(log_path)
    attempts: dict[str, list[int]] = defaultdict(list)
    for r in records:
        attempts[r["event_id"]].append(int(r.get("delivery_attempt") or 0))
    with factory() as db:
        events = {
            eid: status
            for eid, status in db.execute(select(VisitEvent.id, VisitEvent.persistence_status))
        }
        outbox_delivered = {
            eid
            for (eid,) in db.execute(
                select(EventDeliveryOutbox.event_id).where(
                    EventDeliveryOutbox.status == "DELIVERED"
                )
            )
        }
    missing = sorted(e for e in outbox_delivered if e not in attempts)
    unknown = sorted(e for e in attempts if e not in events)
    redelivered = sorted(e for e, a in attempts.items() if len(a) > 1 and len(set(a)) == len(a))
    duplicated = sorted(e for e, a in attempts.items() if len(set(a)) != len(a))
    pending = sorted(e for e, s in events.items() if s == "PENDING")
    dead = sorted(e for e, s in events.items() if s == "DEAD")
    return {
        "database_events": len(events),
        "logged_events": len(attempts),
        "log_lines": len(records),
        "malformed_lines": malformed,
        "missing_in_log": missing[:50],
        "missing_in_log_count": len(missing),
        "logged_not_in_db": unknown[:50],
        "logged_not_in_db_count": len(unknown),
        "redelivered_count": len(redelivered),
        "unexplained_duplicates": duplicated[:50],
        "unexplained_duplicates_count": len(duplicated),
        "pending_delivery_count": len(pending),
        "dead_count": len(dead),
        "consistent": not missing and not duplicated and not dead,
    }
