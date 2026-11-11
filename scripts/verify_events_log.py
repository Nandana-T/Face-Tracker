r"""Compare ``events.log`` with the database (PostgreSQL is the source of truth) and optionally repair.

    python scripts/verify_events_log.py            report only (exit code 1 when inconsistent)
    python scripts/verify_events_log.py --repair   also run event reconciliation first: deliver
                                                   missing log lines, retry evidence, remove orphans,
                                                   close visits left open by a crash

Reads DATABASE_URL / CONFIG_FILE like the API does. The report never contains embeddings, image paths
or credentials. A redelivered line (``delivery_attempt`` > 1) is EXPLAINED - a crash between writing
the line and acknowledging it; the same attempt twice is a defect.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.config import load_app_config  # noqa: E402
from app.core.logging import setup_logging  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.db.session import create_db_engine, create_session_factory  # noqa: E402
from app.services.event_audit import verify_events_log  # noqa: E402
from app.services.event_recorder import EventRecorder  # noqa: E402
from app.services.evidence_store import EvidenceStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="run reconciliation before verifying")
    args = parser.parse_args()
    settings = get_settings()
    config = load_app_config(settings.config_path)
    setup_logging("WARNING", settings.log_path, config.events_log_path)
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        repaired: dict[str, int] | None = None
        if args.repair:
            recorder = EventRecorder(factory, EvidenceStore(config.evidence_path), config.visits)
            repaired = recorder.reconcile(set(), startup=True)
        report = verify_events_log(factory, config.events_log_path)
        out = {"reconciliation": repaired, "verification": report}
        print(json.dumps(out, indent=2))
        return 0 if report["consistent"] else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
