r"""Analytics query latency as the data grows (SYNTHETIC rows on a throw-away PostgreSQL schema).

    .venv\Scripts\python.exe backend\benchmarks\benchmark_analytics.py --out datasets\evidence\analytics-perf

Never touches real visitor data and never the hosted database: it uses the embedded local PostgreSQL in a
schema that is dropped at the end. Row counts are synthetic and say nothing about people.
Measured per size: every analytics query (all-time, date-filtered, source-filtered, session-filtered),
the independent reconciliation, EXPLAIN of the heaviest query, process memory.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from pydantic import SecretStr  # noqa: E402
from sqlalchemy import insert, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.settings import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import create_db_engine  # noqa: E402
from app.models import IdentityAssociation, ProcessingSession, Visit, VisitEvent, Visitor  # noqa: E402
from app.services.analytics import AnalyticsService, Scope  # noqa: E402

T0 = datetime(2026, 1, 1, tzinfo=UTC)
SOURCES = ["hall", "gate", "lab", "lobby", "dock"]


def populate(engine: Any, visits: int, seed: int = 1) -> dict[str, int]:
    rnd = random.Random(seed)  # noqa: S311
    n_visitors = max(10, visits // 4)
    n_sessions = max(5, visits // 50)
    sessions = [f"{i:032x}" for i in range(1, n_sessions + 1)]
    with engine.begin() as conn:
        conn.execute(
            insert(ProcessingSession),
            [
                {
                    "id": sid,
                    "source_id": SOURCES[i % len(SOURCES)],
                    "source_label": SOURCES[i % len(SOURCES)].title(),
                    "source_type": "file",
                    "state": "COMPLETED",
                    "frames_read": 0,
                    "frames_processed": 0,
                    "frames_skipped": 0,
                    "frames_dropped": 0,
                    "corrupt_frames": 0,
                    "detections_total": 0,
                    "max_faces_in_frame": 0,
                    "created_at": T0 + timedelta(hours=i * 3),
                    "summary": {
                        "tracks_confirmed_total": 40,
                        "recog_processed": 100,
                        "recog_usable": 30,
                    },
                }
                for i, sid in enumerate(sessions)
            ],
        )
        conn.execute(
            insert(Visitor),
            [
                {"id": f"{v:032x}", "visitor_number": v, "registration_key": f"k{v}", "status": "REGISTERED",
                 "model_version": "m", "first_session_id": rnd.choice(sessions), "recognition_count": 0}
                for v in range(1, n_visitors + 1)
            ],
        )  # fmt: skip
        v_rows, e_rows, a_rows = [], [], []
        for n in range(1, visits + 1):
            sid = rnd.choice(sessions)
            idx = sessions.index(sid) if n < 2000 else int(sid, 16) - 1
            src = SOURCES[idx % len(SOURCES)]
            vid = f"{rnd.randint(1, n_visitors):032x}"
            entry = T0 + timedelta(hours=idx * 3, seconds=rnd.randrange(0, 10_000))
            end = rnd.choice(["absence", "observation_ended", "interrupted"])
            exit_t = entry + timedelta(seconds=rnd.randrange(5, 300))
            visit_id = f"{n:032x}"
            v_rows.append(
                {"id": visit_id, "visit_key": f"k{n}", "visitor_id": vid, "visitor_number": 1, "session_id": sid,
                 "source_id": src, "source_label": src.title(),
                 "state": "INTERRUPTED" if end == "interrupted" else "CLOSED", "entry_time": entry,
                 "entry_video_ms": 0, "last_observed_time": exit_t, "last_observed_video_ms": 0, "exit_time": exit_t,
                 "exit_video_ms": 1000, "exit_basis": end, "closure_reason": "x", "track_segments": 1}
            )  # fmt: skip
            e_rows.append(
                {"id": f"a{n:031x}", "visit_id": visit_id, "visitor_id": vid, "visitor_number": 1, "event_type": "entry",
                 "slot": "entry", "idempotency_key": f"{visit_id}:entry", "payload_hash": "h", "event_time": entry,
                 "video_ms": 0, "image_status": "STORED", "persistence_status": "DELIVERED"}
            )  # fmt: skip
            e_rows.append(
                {"id": f"b{n:031x}", "visit_id": visit_id, "visitor_id": vid, "visitor_number": 1,
                 "event_type": "interrupted" if end == "interrupted" else "exit", "slot": "terminal",
                 "idempotency_key": f"{visit_id}:terminal", "payload_hash": "h", "event_time": exit_t, "video_ms": 1000,
                 "image_status": "STORED", "persistence_status": "DELIVERED"}
            )  # fmt: skip
            if n % 5 == 0:
                a_rows.append(
                    {"id": uuid.uuid4().hex, "session_id": sid, "track_id": n, "visitor_id": vid,
                     "state": rnd.choice(["RECOGNIZED", "REGISTERED", "INSUFFICIENT_QUALITY", "AMBIGUOUS"]),
                     "observations": 3, "conflicts": 0, "registered_here": 0}
                )  # fmt: skip
        for rows, model in ((v_rows, Visit), (e_rows, VisitEvent), (a_rows, IdentityAssociation)):
            for i in range(0, len(rows), 5000):
                conn.execute(insert(model), rows[i : i + 5000])
        conn.execute(text("ANALYZE"))
    return {"visits": visits, "events": visits * 2, "visitors": n_visitors, "sessions": n_sessions}


def timed(fn: Any, reps: int = 7) -> dict[str, float]:
    fn()  # warm-up
    ms = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        ms.append((time.perf_counter() - t) * 1000)
    return {
        "median_ms": round(statistics.median(ms), 1),
        "p95_ms": round(sorted(ms)[int(len(ms) * 0.95) - 1], 1),
        "max_ms": round(max(ms), 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sizes", default="1000,10000,100000")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    import pgserver

    settings = get_settings()
    url = SecretStr(
        pgserver.get_server(ROOT / "storage" / "pgdata", cleanup_mode=None)
        .get_uri()
        .replace("postgresql://", "postgresql+psycopg://", 1)
    )
    admin = create_db_engine(settings.model_copy(update={"database_url": url}))
    proc = psutil.Process()
    results: list[dict[str, Any]] = []
    for size in [int(s) for s in args.sizes.split(",")]:
        schema = f"perf_{uuid.uuid4().hex[:10]}"
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_db_engine(
            settings.model_copy(update={"database_url": url}),
            connect_args={"options": f"-csearch_path={schema}"},
        )
        try:
            Base.metadata.create_all(engine)
            t0 = time.perf_counter()
            counts = populate(engine, size)
            load_s = round(time.perf_counter() - t0, 1)
            svc = AnalyticsService(sessionmaker(engine, expire_on_commit=False), lambda: set())
            since, until = T0 + timedelta(hours=100), T0 + timedelta(hours=400)
            session_id = f"{3:032x}"
            rss0 = proc.memory_info().rss
            q = {
                "overview_all_time": timed(lambda: svc.overview(Scope())),
                "overview_date_range": timed(lambda: svc.overview(Scope(since=since, until=until))),
                "overview_one_source": timed(lambda: svc.overview(Scope(source_id="gate"))),
                "overview_one_session": timed(lambda: svc.overview(Scope(session_id=session_id))),
                "timeline_day": timed(lambda: svc.timeline(Scope(), "day")),
                "timeline_hour_range": timed(
                    lambda: svc.timeline(Scope(since=since, until=until), "hour")
                ),
                "sources": timed(lambda: svc.sources(Scope())),
                "sessions_page": timed(lambda: svc.sessions(Scope(), 20, 0)),
                "coverage": timed(lambda: svc.coverage(Scope())),
                "recent": timed(lambda: svc.recent(Scope(), 10)),
            }
            rec = svc.reconcile()
            q["reconcile_full_recount"] = {
                "median_ms": rec["duration_ms"],
                "p95_ms": rec["duration_ms"],
                "max_ms": rec["duration_ms"],
            }
            with engine.connect() as conn:
                plan = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "EXPLAIN (ANALYZE, BUFFERS) SELECT count(distinct visits.visitor_id) FROM visits "
                            "JOIN visitors ON visitors.id = visits.visitor_id WHERE visits.entry_time >= :s AND visits.entry_time < :u "
                            "AND visits.session_id <> visitors.first_session_id"
                        ),
                        {"s": since, "u": until},
                    )
                ]
                plan_source = [
                    r[0]
                    for r in conn.execute(
                        text(
                            "EXPLAIN (ANALYZE) SELECT count(*) FROM visits WHERE source_id = 'gate'"
                        )
                    )
                ]
            results.append(
                {
                    **counts,
                    "load_s": load_s,
                    "queries": q,
                    "reconciliation_consistent": rec["consistent"],
                    "python_rss_growth_mb": round((proc.memory_info().rss - rss0) / 2**20, 1),
                    "explain_returning_range": plan[:3] + plan[-2:],
                    "explain_source_filter": plan_source[:2] + plan_source[-2:],
                }
            )
            print(
                json.dumps({"visits": size, **{k: v["median_ms"] for k, v in q.items()}}),
                flush=True,
            )
        finally:
            engine.dispose()
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()
    (args.out / "analytics-perf.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
