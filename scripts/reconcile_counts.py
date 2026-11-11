r"""Recompute the global visitor/visit/event counts independently and report differences.

    python scripts/reconcile_counts.py            human-readable report; exit code 1 when an ERROR check fails
    python scripts/reconcile_counts.py --json     machine-readable

REPORT-ONLY: this tool never writes to the database. There is deliberately no repair mode - a count that
disagrees with the rows is a finding to investigate (the event-level repairs live in
``scripts/verify_events_log.py --repair`` / ``POST /api/v1/events/reconcile`` and are explicit).
Reads DATABASE_URL like the API. Checks: registered visitors, unique visitors, visits, entry/terminal
events against raw rows; one entry and the right terminal events per visit; foreign keys; duplicate
idempotency keys; closure-basis consistency; erased-visitor handling; stale open visits; pending outbox.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.settings import get_settings  # noqa: E402
from app.db.session import create_db_engine, create_session_factory  # noqa: E402
from app.services.analytics import AnalyticsService  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    engine = create_db_engine(get_settings())
    try:
        # No API process is asked, so no session counts as live: open visits are reported as stale.
        report = AnalyticsService(create_session_factory(engine)).reconcile()
    finally:
        engine.dispose()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"{report['mode']}  ({report['duration_ms']} ms)")
        for c in report["checks"]:
            mark = "ok  " if c["ok"] else ("WARN" if c["severity"] == "warning" else "FAIL")
            print(f"[{mark}] {c['name']}: expected {c['expected']} actual {c['actual']}  {c['detail']}")
        print(f"consistent={report['consistent']} errors={report['errors']} warnings={report['warnings']}")
    return 0 if report["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
