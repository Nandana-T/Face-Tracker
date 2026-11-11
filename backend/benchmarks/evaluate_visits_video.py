r"""Visit lifecycle on the real hackathon footage: coverage, event integrity, latency and cost.

    .venv\Scripts\python.exe backend\benchmarks\evaluate_visits_video.py run --out datasets\evidence\visits
    .venv\Scripts\python.exe backend\benchmarks\evaluate_visits_video.py ab  --out datasets\evidence\visits

The footage has NO identity labels, so nothing here is a people count or an accuracy figure. What is
measured (without labels):
  * how many visits open, how they end (absence / observation ended / interrupted) and how many
    tracks never became a visit because the recognition quality gate (unchanged) rejected the faces
  * integrity of the produced events: entry<=exit, one entry + one terminal event per visit, evidence
    present, events.log vs database consistency, reconciliation result
  * persistence latency (database commit, image write, events.log line) on a real PostgreSQL schema
  * lifecycle cost per processed frame, and throughput / CPU / memory with and without the visit layer
and saves evidence (entry/exit crops with times) for MANUAL review. Evidence shows real people and is
git-ignored; do not share it.

The harness mirrors the API: the pipeline's per-frame track metadata (sampled at the stream cadence in
VIDEO time) feeds the VisitManager, recognition observations feed the IdentityManager, and each
resulting entry/terminal action is recorded by the EventRecorder (here synchronously so that the
latencies can be attributed; the API does the same work off the frame path).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import psutil

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import load_app_config  # noqa: E402
from app.core.logging import _reset, setup_logging  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import create_db_engine  # noqa: E402
from app.models import ProcessingSession, Visit, VisitEvent  # noqa: E402
from app.services.event_audit import verify_events_log  # noqa: E402
from app.services.event_recorder import EventRecorder  # noqa: E402
from app.services.evidence_store import EvidenceStore  # noqa: E402
from app.services.identity_manager import IdentityManager, Observation  # noqa: E402
from app.services.image_store import LocalImageStore  # noqa: E402
from app.services.visit_manager import TrackSnap, VisitAction, VisitManager  # noqa: E402
from app.vision.detection.model_registry import YOLOV11N_FACE, model_path  # noqa: E402
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector  # noqa: E402
from app.vision.recognition import registry as R  # noqa: E402
from app.vision.recognition.service import (  # noqa: E402
    FaceRecognizer,
    RecognitionRunner,
    RecognitionScheduler,
)
from app.vision.sources.file_source import FileVideoSource  # noqa: E402
from app.vision.tracking.tracker import FaceByteTracker  # noqa: E402
from app.workers.pipeline import PipelineSettings, ProcessingPipeline  # noqa: E402

FOOTAGE = ROOT / "datasets" / "hackathon"
CLIPS = [
    "record_20250620_183903.mp4",
    "record_20250620_184807.mp4",
    "record_20250620_185210.mp4",
    "record_20250620_184403.mp4",
    "record_20250620_190324.mp4",
    "video_sample1.mp4",
]
STREAM_FPS = 15  # the default stream cadence: the API sees track metadata at about this rate


class Rig:
    """Isolated PostgreSQL schema + identity manager + visit manager + recorder (production code)."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        settings = get_settings()
        config = load_app_config(ROOT / "config" / "config.json")
        self.cfg = config
        url = _local_pg_url()
        self.schema = f"eval_{uuid.uuid4().hex[:10]}"
        self.admin = create_db_engine(settings.model_copy(update={"database_url": url}))
        with self.admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_db_engine(
            settings.model_copy(update={"database_url": url}),
            connect_args={"options": f"-csearch_path={self.schema}"},
        )
        Base.metadata.create_all(self.engine)  # identical to the migration (drift test)
        self.factory = sessionmaker(self.engine, expire_on_commit=False)
        self.log_path = tmp / "events.log"
        setup_logging("WARNING", tmp / "app-logs", self.log_path)
        self.identity = IdentityManager(
            self.factory,
            LocalImageStore(tmp / "faces"),
            config.recognition,
            R.SFACE.key,
            R.SFACE.embedding_dim,
        )
        self.identity.load()
        self.visits = VisitManager(config.visits)
        self.store = EvidenceStore(tmp / "evidence")
        self.recorder = EventRecorder(self.factory, self.store, config.visits)

    def close(self) -> None:
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin.dispose()
        _reset(__import__("logging").getLogger())
        _reset(__import__("logging").getLogger("katomaran.events"))


def _local_pg_url() -> Any:
    import pgserver
    from pydantic import SecretStr

    # The embedded local PostgreSQL (never the hosted database); each run uses a throw-away schema.
    server = pgserver.get_server(ROOT / "storage" / "pgdata", cleanup_mode=None)
    return SecretStr(server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1))


def run_clip(
    rig: Rig, name: str, models: Path, with_visits: bool = True, max_frames: int | None = None
) -> dict[str, Any]:
    cfg = rig.cfg
    sid = uuid.uuid4().hex
    with rig.factory() as db:
        db.add(
            ProcessingSession(
                id=sid, source_id=name, source_label=name, source_type="file", state="RUNNING",
                frames_read=0, frames_processed=0, frames_skipped=0, frames_dropped=0,
                corrupt_frames=0, detections_total=0, max_faces_in_frame=0,
            )
        )  # fmt: skip
        db.commit()
    rig.visits.begin_session(sid, name, name)
    on_frame_us: list[float] = []
    actions: list[VisitAction] = []
    reasons: Counter[str] = Counter()
    last_fed = -10_000
    confirmed_tracks: set[int] = set()
    bound_tracks: set[int] = set()
    last_ms = [0]

    def feed(res: Any) -> None:
        nonlocal last_fed
        if not with_visits:
            return
        ms = res.timestamp_ms
        last_ms[0] = ms
        if ms - last_fed < 1000 / STREAM_FPS:
            return  # the API only sees frames at the stream cadence
        last_fed = ms
        snaps = [TrackSnap(t.track_id, t.state, t.observed, t.first_seen_ms) for t in res.tracks]
        confirmed_tracks.update(t.track_id for t in res.tracks if t.state == "CONFIRMED")
        t0 = time.perf_counter()
        out = rig.visits.on_frame(sid, res.frame_id, ms, datetime.now(UTC), snaps)
        on_frame_us.append((time.perf_counter() - t0) * 1e6)
        actions.extend(out)

    def on_face(r: Any) -> None:
        if r.embedding is None:
            for reason in r.reasons or ("rejected",):
                reasons[reason] += 1
        if with_visits and r.embedding is not None:
            rig.visits.on_observation(sid, r.track_id, r.crop_jpeg, r.quality, r.timestamp_ms)
        events = rig.identity.observe(
            Observation(sid, r.track_id, r.seq, R.SFACE.key, r.embedding, r.quality, r.reasons,
                        r.timestamp_ms, r.frame_id, r.crop_jpeg)
        )  # fmt: skip
        if not with_visits:
            return
        for e in events:
            if e.visitor_id is not None:
                bound_tracks.add(e.track_id)
                visitor, number = e.visitor_id, e.visitor_number
            elif e.state in ("AMBIGUOUS", "REJECTED"):
                visitor, number = None, None
            else:
                continue
            actions.extend(
                rig.visits.on_identity(
                    sid, e.track_id, visitor, number, r.timestamp_ms, datetime.now(UTC)
                )
            )

    recognizer = FaceRecognizer(models, cfg.recognition)
    runner = RecognitionRunner(
        recognizer, on_face, RecognitionScheduler(cfg.recognition), cfg.recognition.queue_size
    )
    detector = YoloOnnxFaceDetector(
        model_path(models, YOLOV11N_FACE), intra_op_threads=4, allow_spinning=False
    )
    source = FileVideoSource(FOOTAGE / name)
    if max_frames:
        orig = source.frames

        def limited() -> Any:
            for k, f in enumerate(orig()):
                if k >= max_frames:
                    return
                yield f

        source.frames = limited  # type: ignore[method-assign]
    pipe = ProcessingPipeline(
        source, detector,
        PipelineSettings(frame_skip=2, realtime_pacing=False, stream_max_fps=1),
        on_result=lambda res, _jpeg: feed(res), tracker=FaceByteTracker(), recognition=runner,
        recognition_pad=cfg.recognition.landmark_pad,
    )  # fmt: skip
    proc = psutil.Process()
    cpu0 = proc.cpu_times()
    rss_peak = 0
    stop = threading.Event()

    def sample() -> None:
        nonlocal rss_peak
        while not stop.is_set():
            rss_peak = max(rss_peak, proc.memory_info().rss)
            time.sleep(0.05)

    threading.Thread(target=sample, daemon=True).start()
    t0 = time.perf_counter()
    outcome = pipe.run()
    wall = time.perf_counter() - t0
    stop.set()
    cpu1 = proc.cpu_times()

    # end of the clip = normal end of stream (observation ended), exactly like the API
    if with_visits:
        actions.extend(rig.visits.end_session(sid, "end_of_stream", datetime.now(UTC)))
    rig.identity.end_session(sid)
    record_ms: list[float] = []
    errors = 0
    for a in actions:
        if a.kind not in ("entry", "terminal"):
            continue
        t1 = time.perf_counter()
        try:
            rig.recorder.record(a)
        except Exception:  # noqa: BLE001
            errors += 1
        record_ms.append((time.perf_counter() - t1) * 1000)
    st = pipe.stats
    entries = [a for a in actions if a.kind == "entry"]
    terminals = [a for a in actions if a.kind == "terminal"]
    return {
        "clip": name,
        "outcome": outcome.state,
        "analysed_frames": st.frames_processed,
        "wall_s": round(wall, 1),
        "cpu_s": round((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system), 1),
        "peak_rss_mb": round(rss_peak / 2**20),
        "avg_detector_ms": round(st._inference_total_ms / max(1, st.frames_processed), 1),  # noqa: SLF001
        "confirmed_tracks": len(confirmed_tracks),
        "tracks_bound_to_a_visitor": len(bound_tracks),
        "visits_opened": len(entries),
        "terminal_events": Counter(f"{a.event_type}/{a.visit.exit_basis}" for a in terminals),
        "quality_rejection_reasons": dict(reasons),
        "on_frame_us_mean": round(statistics.fmean(on_frame_us), 1) if on_frame_us else None,
        "on_frame_us_p95": round(float(np.percentile(on_frame_us, 95)), 1) if on_frame_us else None,
        "on_frame_us_max": round(max(on_frame_us), 1) if on_frame_us else None,
        "events_recorded": len(record_ms),
        "record_errors": errors,
        "record_ms_mean": round(statistics.fmean(record_ms), 1) if record_ms else None,
        "record_ms_max": round(max(record_ms), 1) if record_ms else None,
        "visit_spans_s": [
            round((a.visit.last_video_ms - a.visit.entry_video_ms) / 1000, 1) for a in terminals
        ],
    }


def contact_sheet(rig: Rig, out: Path) -> None:
    """Entry (top) and exit (bottom) crop of every visit, with times, for manual review."""
    out.mkdir(parents=True, exist_ok=True)
    with rig.factory() as db:
        visits = db.execute(select(Visit).order_by(Visit.entry_time)).scalars().all()
        events = {(e.visit_id, e.slot): e for e in db.execute(select(VisitEvent)).scalars().all()}
    tiles = []
    for v in visits:
        col = []
        for slot in ("entry", "terminal"):
            e = events.get((v.id, slot))
            img = np.zeros((176, 144, 3), np.uint8)
            if e is not None and e.image_path and rig.store.exists(e.image_path):
                raw = cv2.imdecode(
                    np.frombuffer(rig.store.read(e.image_path), np.uint8), cv2.IMREAD_COLOR
                )
                img = cv2.resize(raw, (144, 176))
            label = f"V{v.visitor_number:05d} {slot[:4]} {(e.video_ms if e else 0) / 1000:.0f}s"
            cv2.putText(img, label, (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
            col.append(img)
        tiles.append(np.vstack(col))
    if not tiles:
        return
    cols = 10
    rows = [
        np.hstack(
            tiles[i : i + cols] + [np.zeros_like(tiles[0])] * (cols - len(tiles[i : i + cols]))
        )
        for i in range(0, len(tiles), cols)
    ]
    cv2.imwrite(str(out / "visit_entry_exit_sheet.jpg"), np.vstack(rows))


def cmd_run(out: Path, exit_after: float | None = None) -> None:
    models = get_settings().models_path
    tmp = Path(tempfile.mkdtemp(prefix="visits-video-", dir=ROOT / "temp"))
    rig = Rig(tmp)
    if exit_after is not None:  # sensitivity study: another grace period, everything else identical
        visits_cfg = rig.cfg.visits.model_copy(
            update={"exit_after_seconds": exit_after, "missing_after_seconds": min(1.5, exit_after)}
        )
        rig.cfg = rig.cfg.model_copy(update={"visits": visits_cfg})
        rig.visits = VisitManager(visits_cfg)
    try:
        summaries = []
        for clip in CLIPS:
            s = run_clip(rig, clip, models)
            summaries.append(s)
            print(
                json.dumps(
                    {
                        k: s[k]
                        for k in ("clip", "confirmed_tracks", "visits_opened", "terminal_events")
                    }
                ),
                flush=True,
            )
        # integrity: counts, ordering, evidence, events.log, reconciliation
        with rig.factory() as db:
            n_visits = int(db.execute(select(func.count()).select_from(Visit)).scalar_one())
            by_type = {
                t: int(n)
                for t, n in db.execute(
                    select(VisitEvent.event_type, func.count()).group_by(VisitEvent.event_type)
                )
            }
            images = {
                s: int(n)
                for s, n in db.execute(
                    select(VisitEvent.image_status, func.count()).group_by(VisitEvent.image_status)
                )
            }
            slots = db.execute(
                select(VisitEvent.visit_id, func.count()).group_by(VisitEvent.visit_id)
            ).all()
            bad_order = int(
                db.execute(
                    select(func.count())
                    .select_from(Visit)
                    .where(Visit.exit_time < Visit.entry_time)
                ).scalar_one()
            )
            visitors = int(
                db.execute(select(func.count(func.distinct(Visit.visitor_id)))).scalar_one()
            )
        verification = verify_events_log(rig.factory, rig.log_path)
        rec1 = rig.recorder.reconcile(set(), startup=True)
        rec2 = rig.recorder.reconcile(set(), startup=True)
        contact_sheet(rig, out)
        report = {
            "thresholds": {
                "missing_after_s": rig.cfg.visits.missing_after_seconds,
                "exit_after_s": rig.cfg.visits.exit_after_seconds,
                "max_frame_gap_s": rig.cfg.visits.max_frame_gap_seconds,
                "stream_cadence_fps": STREAM_FPS,
            },
            "clips": summaries,
            "totals": {
                "visits": n_visits,
                "events_by_type": by_type,
                "events_by_image_status": images,
                "visits_with_exactly_two_events": sum(1 for _, n in slots if n == 2),
                "visits_with_other_event_counts": sum(1 for _, n in slots if n != 2),
                "exit_before_entry": bad_order,
                "distinct_visitors_with_a_visit": visitors,
                "registry_visitors": rig.identity.visitor_count(),
            },
            "events_log_vs_database": verification,
            "reconciliation_first_pass": rec1,
            "reconciliation_second_pass": rec2,
            "recorder_latency": {k: t.summary() for k, t in rig.recorder.timing.items()},
            "note": "No identity labels exist for this footage: visit counts are not people counts.",
        }
        name = (
            "visits-results.json"
            if exit_after is None
            else f"visits-results-exit{exit_after:g}.json"
        )
        (out / name).write_text(json.dumps(report, indent=2, default=str))
        print(json.dumps(report["totals"], indent=2, default=str))
    finally:
        rig.close()


def cmd_ab(out: Path) -> None:
    """Throughput / CPU / memory with and without the visit layer (recognition on in both)."""
    models = get_settings().models_path
    result: dict[str, Any] = {}
    for clip in ("record_20250620_183903.mp4", "video_sample1.mp4"):
        runs: dict[str, list[dict[str, Any]]] = {"without": [], "with": []}
        for rep in range(3):
            for label, flag in (("without", False), ("with", True)):
                tmp = Path(tempfile.mkdtemp(prefix="visits-ab-", dir=ROOT / "temp"))
                rig = Rig(tmp)
                try:
                    s = run_clip(rig, clip, models, with_visits=flag, max_frames=600)
                finally:
                    rig.close()
                runs[label].append(s)
                print(clip, rep, label, s["wall_s"], s["cpu_s"], s["peak_rss_mb"], flush=True)
        result[clip] = {
            label: {
                k: round(statistics.median(x[k] for x in rs), 2)
                for k in ("wall_s", "cpu_s", "peak_rss_mb", "avg_detector_ms")
            }
            for label, rs in runs.items()
        } | {"runs": runs}
    (out / "visits-ab.json").write_text(json.dumps(result, indent=2, default=str))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["run", "ab"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument(
        "--exit-after", type=float, default=None, help="grace period for the sensitivity study"
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.command == "run":
        cmd_run(args.out, args.exit_after)
    else:
        cmd_ab(args.out)


if __name__ == "__main__":
    main()
