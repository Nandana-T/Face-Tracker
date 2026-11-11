r"""Recognition on the real hackathon footage: behaviour, cost and qualitative evidence.

    .venv\Scripts\python.exe backend\benchmarks\evaluate_recognition_video.py run --out datasets\evidence\recognition\video
    .venv\Scripts\python.exe backend\benchmarks\evaluate_recognition_video.py ab  --out datasets\evidence\recognition\video

The footage has NO identity labels, so this script cannot - and does not - report identity accuracy.
It reports what is measurable without labels:
  * how many faces pass the quality gate, and why the others do not
  * how tracks are resolved (registered / recognised / undecided / ambiguous / low quality)
  * visitors registered per clip and across clips (a registry that keeps growing when the same few
    people walk by is a SIGN of duplicates, but not proof - hence "suspect", never "error")
  * latency / CPU / memory cost with and without recognition
and saves evidence images (visitor crops, annotated frames) for MANUAL review. Evidence shows real
people and is git-ignored; it must not be shared.

All clips run through ONE registry in sequence, so a person who appears in several clips can be
recognised across clips (the "repeated appearances" case) - whether such a match is correct is a
human judgement made from the saved evidence images.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import psutil

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import load_app_config  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import ProcessingSession, Visitor  # noqa: E402
from app.services.identity_manager import IdentityManager, Observation  # noqa: E402
from app.services.image_store import LocalImageStore  # noqa: E402
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


def build(tmp: Path) -> tuple[IdentityManager, Any, Any]:
    cfg = load_app_config(ROOT / "config" / "config.json").recognition
    engine = create_engine(f"sqlite:///{tmp / 'eval.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    mgr = IdentityManager(
        factory, LocalImageStore(tmp / "faces"), cfg, R.SFACE.key, R.SFACE.embedding_dim
    )
    mgr.load()
    return mgr, factory, cfg


def run_clip(
    name: str,
    mgr: IdentityManager,
    factory: Any,
    models: Path,
    out: Path | None,
    recognition: bool = True,
    max_frames: int | None = None,
) -> dict[str, Any]:
    cfg = mgr.cfg
    sid = (name.replace(".mp4", "") + "0" * 32)[:32].replace("_", "0")
    with factory() as db:
        db.add(
            ProcessingSession(
                id=sid,
                source_id="x",
                source_label=name,
                source_type="file",
                state="COMPLETED",
                frames_read=0,
                frames_processed=0,
                frames_skipped=0,
                frames_dropped=0,
                corrupt_frames=0,
                detections_total=0,
                max_faces_in_frame=0,
            )
        )
        db.commit()
    reasons: Counter[str] = Counter()
    results = {"n": 0, "usable": 0, "ms": []}
    events: Counter[str] = Counter()
    detections: dict[int, Any] = {}

    def on_face(r: Any) -> None:
        results["n"] += 1
        results["ms"].append(r.process_ms)
        if r.embedding is not None:
            results["usable"] += 1
        else:
            for reason in r.reasons or ("rejected",):
                reasons[reason] += 1
        for e in mgr.observe(
            Observation(
                sid,
                r.track_id,
                r.seq,
                R.SFACE.key,
                r.embedding,
                r.quality,
                r.reasons,
                r.timestamp_ms,
                r.frame_id,
                r.crop_jpeg,
            )
        ):
            events[e.kind] += 1

    recognizer = FaceRecognizer(models, cfg) if recognition else None
    runner = (
        RecognitionRunner(recognizer, on_face, RecognitionScheduler(cfg), cfg.queue_size)
        if recognizer
        else None
    )
    detector = YoloOnnxFaceDetector(
        model_path(models, YOLOV11N_FACE), intra_op_threads=4, allow_spinning=False
    )

    def on_result(res: Any, jpeg: Any) -> None:
        detections[res.frame_id] = res

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
        on_result=on_result, tracker=FaceByteTracker(), recognition=runner,
        recognition_pad=cfg.landmark_pad,
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

    # final states of this clip's tracks (snapshot before end_session frees them)
    states: Counter[str] = Counter()
    confirmed_tracks = 0
    for (s, _tid), ts in list(mgr._tracks.items()):  # noqa: SLF001
        if s == sid:
            states[ts.state] += 1
            confirmed_tracks += 1
    if out is not None:
        save_evidence(name, sid, mgr, factory, detections, out)
    mgr.end_session(sid)
    st = pipe.stats
    summary = {
        "clip": name,
        "outcome": outcome.state,
        "analysed_frames": st.frames_processed,
        "wall_s": round(wall, 1),
        "cpu_s": round((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system), 1),
        "peak_rss_mb": round(rss_peak / 2**20),
        "confirmed_tracks_with_observations": confirmed_tracks,
        "track_identity_states": dict(states),
        "faces_submitted": runner.stats.submitted if runner else 0,
        "faces_dropped_busy": runner.stats.dropped if runner else 0,
        "faces_processed": results["n"],
        "faces_usable_embedding": results["usable"],
        "quality_rejection_reasons": dict(reasons),
        "identity_events": dict(events),
        "recognition_ms_mean": round(statistics.fmean(results["ms"]), 1) if results["ms"] else None,
        "recognition_ms_p95": round(float(np.percentile(results["ms"], 95)), 1)
        if results["ms"]
        else None,
        "avg_detector_ms": round(st._inference_total_ms / max(1, st.frames_processed), 1),  # noqa: SLF001
        "visitors_in_registry_after": mgr.visitor_count(),
    }
    return summary


def save_evidence(
    name: str, sid: str, mgr: IdentityManager, factory: Any, detections: dict[int, Any], out: Path
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    stem = Path(name).stem
    # 1) the representative crop of every visitor registered in THIS clip, as one contact sheet
    tiles: list[np.ndarray] = []
    with factory() as db:
        for v in db.execute(
            select(Visitor).where(Visitor.first_session_id == sid).order_by(Visitor.visitor_number)
        ).scalars():
            if not v.image_path:
                continue
            img = cv2.imdecode(
                np.frombuffer(mgr._store.read(v.image_path), np.uint8), cv2.IMREAD_COLOR
            )  # noqa: SLF001
            tile = cv2.resize(img, (160, 200))
            cv2.putText(
                tile,
                f"V{v.visitor_number:05d}",
                (6, 22),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 255),
                2,
            )
            tiles.append(tile)
    if tiles:
        cols = 8
        rows = [
            np.hstack(
                tiles[i : i + cols] + [np.zeros_like(tiles[0])] * (cols - len(tiles[i : i + cols]))
            )
            for i in range(0, len(tiles), cols)
        ]
        cv2.imwrite(str(out / f"{stem}_registered_visitors.jpg"), np.vstack(rows))
    # 2) annotated full frames at a few analysed frame ids that carry tracks
    ids = sorted(f for f, r in detections.items() if r.tracks)
    picks = [ids[len(ids) * k // 5] for k in (1, 2, 3, 4)] if len(ids) >= 5 else ids
    cap = cv2.VideoCapture(str(FOOTAGE / name))
    for fid in picks:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fid)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        scale = 1280 / w
        img = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        for t in detections[fid].tracks:
            ts = mgr._tracks.get((sid, t.track_id))  # noqa: SLF001
            x1, y1, x2, y2 = (int(v * scale) for v in t.bbox)
            if ts is None or t.state != "CONFIRMED":
                color, label = (160, 160, 160), f"#{t.track_id}"
            elif ts.state in ("RECOGNIZED", "REGISTERED") and ts.visitor_number:
                color = (250, 130, 170) if ts.state == "RECOGNIZED" else (190, 210, 40)
                label = f"V{ts.visitor_number:05d} #{t.track_id}"
            else:
                color, label = (0, 190, 255), f"#{t.track_id} {ts.state.lower()}"
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
            cv2.putText(img, label, (x1, max(14, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
        cv2.imwrite(str(out / f"{stem}_f{fid:05d}.jpg"), img)
    cap.release()


def cmd_run(out: Path) -> None:
    models = get_settings().models_path
    tmp = Path(tempfile.mkdtemp(prefix="recog-video-", dir=ROOT / "temp"))
    mgr, factory, _ = build(tmp)
    t0 = time.perf_counter()
    FaceRecognizer(models, mgr.cfg)  # warm model load timing
    load_s = time.perf_counter() - t0
    summaries = []
    for clip in CLIPS:
        s = run_clip(clip, mgr, factory, models, out)
        summaries.append(s)
        print(
            json.dumps(
                {
                    k: s[k]
                    for k in (
                        "clip",
                        "confirmed_tracks_with_observations",
                        "track_identity_states",
                        "faces_usable_embedding",
                        "quality_rejection_reasons",
                        "visitors_in_registry_after",
                    )
                }
            ),
            flush=True,
        )
    report = {
        "recognition_model_and_landmarks_load_s": round(load_s, 2),
        "thresholds": {
            "similarity": mgr.cfg.similarity_threshold,
            "novelty": mgr.cfg.novelty_threshold,
            "margin": mgr.cfg.ambiguity_margin,
        },
        "clips": summaries,
        "visitors_registered_total": mgr.visitor_count(),
        "manager": mgr.runtime_stats(),
        "note": "No identity labels exist for this footage: nothing here is an accuracy figure.",
    }
    (out / "video-results.json").write_text(json.dumps(report, indent=2))


def cmd_ab(out: Path) -> None:
    models = get_settings().models_path
    result: dict[str, Any] = {}
    for clip in ("record_20250620_183903.mp4", "video_sample1.mp4"):
        runs: dict[str, list[dict[str, Any]]] = {"without": [], "with": []}
        for rep in range(3):
            for label, flag in (("without", False), ("with", True)):
                tmp = Path(tempfile.mkdtemp(prefix="recog-ab-", dir=ROOT / "temp"))
                mgr, factory, _ = build(tmp)
                s = run_clip(clip, mgr, factory, models, None, recognition=flag, max_frames=600)
                runs[label].append(s)
                print(clip, rep, label, s["wall_s"], s["cpu_s"], s["peak_rss_mb"], flush=True)
        med = {
            label: {
                k: round(statistics.median(x[k] for x in rs), 2)
                for k in ("wall_s", "cpu_s", "peak_rss_mb", "avg_detector_ms")
            }
            for label, rs in runs.items()
        }
        result[clip] = {**med, "runs": runs}
    (out / "video-ab.json").write_text(json.dumps(result, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["run", "ab"])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    {"run": cmd_run, "ab": cmd_ab}[args.command](args.out)


if __name__ == "__main__":
    main()
