r"""Write SAFE, reproducible sample outputs into docs/sample-output/ from the local E2E database.

    .venv\Scripts\python.exe scripts\make_sample_output.py

Safety: the E2E fixtures are a public-domain NASA portrait (backend/tests/fixtures/astronaut_nasa_public_domain.png),
never the hackathon footage or any real visitor. The script refuses to run against a non-local database and
publishes only: analytics + reconciliation JSON, visits/events JSON, the matching events.log lines, and the
entry/exit crops of the fixture. No credentials, no embeddings, no storage paths.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config import load_app_config  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.db.session import create_db_engine, create_session_factory  # noqa: E402
from app.models import Visit, VisitEvent  # noqa: E402
from app.services.analytics import AnalyticsService, Scope  # noqa: E402
from app.services.evidence_store import EvidenceStore  # noqa: E402
from app.services.visit_manager import event_id_for  # noqa: E402

OUT = ROOT / "docs" / "sample-output"


def main() -> int:
    url = os.environ.get("DATABASE_URL", "")
    if make_url(url.replace("postgresql://", "postgresql+psycopg://", 1)).host not in ("127.0.0.1", "localhost"):
        print("refusing to export from a non-local database", file=sys.stderr)
        return 2
    cfg = load_app_config(ROOT / "config" / "config.e2e.json")
    engine = create_db_engine(get_settings())
    factory = create_session_factory(engine)
    store = EvidenceStore(ROOT / cfg.visits.evidence_dir)
    shutil.rmtree(OUT, ignore_errors=True)
    (OUT / "images").mkdir(parents=True)
    try:
        svc = AnalyticsService(factory)
        (OUT / "analytics-overview.json").write_text(json.dumps(svc.overview(Scope()), indent=2, default=str))
        (OUT / "reconciliation.json").write_text(json.dumps(svc.reconcile(), indent=2, default=str))
        with factory() as db:
            visits = db.execute(
                select(Visit).join(VisitEvent, VisitEvent.visit_id == Visit.id)
                .where(VisitEvent.image_status == "STORED", Visit.visitor_id.is_not(None), Visit.state == "CLOSED")
                .order_by(Visit.entry_time.desc()).distinct().limit(3)
            ).scalars().all()  # fmt: skip
            rows, ids = [], []
            for v in visits:
                evs = db.execute(select(VisitEvent).where(VisitEvent.visit_id == v.id).order_by(VisitEvent.slot)).scalars().all()
                ids += [e.id for e in evs]
                rows.append({
                    "visit": {"id": v.id, "visitor_label": f"V{(v.visitor_number or 0):05d}", "state": v.state, "source": v.source_label,
                              "entry_time": v.entry_time.isoformat(), "exit_time": v.exit_time.isoformat() if v.exit_time else None,
                              "exit_basis": v.exit_basis, "closure_reason": v.closure_reason},
                    "events": [{"id": e.id, "type": e.event_type, "event_time": e.event_time.isoformat(), "image_status": e.image_status,
                                "image_kind": e.image_kind, "persistence_status": e.persistence_status} for e in evs],
                })  # fmt: skip
                for e in evs:
                    if e.image_status == "STORED" and e.image_path:
                        (OUT / "images" / f"{e.event_type}-{e.id[:8]}.jpg").write_bytes(store.read(e.image_path))
        (OUT / "visits-and-events.json").write_text(json.dumps(rows, indent=2))
        log = cfg.events_log_path
        lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if any(i in ln for i in ids)] if log.exists() else []
        (OUT / "events-log-excerpt.jsonl").write_text("\n".join(lines) + "\n")
        (OUT / "README.md").write_text(
            "# Sample output (synthetic / public-domain fixture)\n\n"
            "Produced by `scripts/make_sample_output.py` from the controlled end-to-end run. The only face is the **public-domain NASA portrait** "
            "used as a test fixture (`backend/tests/fixtures/astronaut_nasa_public_domain.png`). **No hackathon footage, no real visitor and no "
            "credential appears here**; real-footage evidence stays private (git-ignored) and is described only in aggregate in the reports.\n\n"
            "* `analytics-overview.json` - counts (identities, visits, entry/terminal events, active vs stale) with their definitions\n"
            "* `reconciliation.json` - the read-only independent recount\n"
            "* `visits-and-events.json` - sample visits with their entry/exit events\n"
            "* `events-log-excerpt.jsonl` - the matching `events.log` lines (delivery attempt, persistence outcome)\n"
            "* `images/` - timestamped entry/exit crops (entry = best observation; exit = last good observation, not a new capture)\n"
        )
        print(f"wrote {len(rows)} visits, {len(list((OUT / 'images').iterdir()))} images, {len(lines)} log lines")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
