r"""Ground-truth evaluation of unique-identity counting (LFW identities through the REAL pipeline).

    .venv\Scripts\python.exe backend\benchmarks\evaluate_unique_count.py --out datasets\evidence\unique-count

What the labels are. LFW (Labeled Faces in the Wild) is a research benchmark whose folders are VERIFIED
person identities. Clips are composed from LFW photographs (a different photograph per appearance), so every
appearance has a ground-truth person - not merely a track id. The hackathon footage has no such labels and is
NOT used for accuracy claims here.

What is real: detector, tracker, landmarks, SFace embeddings, identity manager, visit lifecycle, PostgreSQL
(isolated schema), analytics aggregation. What is synthetic: the video is a still photograph shown full frame
(zoomed/panned) - no motion blur, no crowd, no camera noise; "occluded" clips paint a mask-like patch over the
lower face; "small" clips show the face at roughly a quarter of the normal size. Results therefore measure
the identity layer under LFW-like conditions and are optimistic for real surveillance footage.

Scenarios: first appearance of every person (single person per clip), returning people (photo 2, photo 3),
pairs of different people in one frame (simultaneous visitors), small faces, occluded faces.
Reported only because labels justify them: ground-truth unique people, correctly registered identities,
missed people, duplicate registrations (one person -> several identities), incorrect merges (one identity ->
several people), unique-count absolute error, returning-visitor agreement.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from evaluate_visits_video import ROOT, Rig  # noqa: E402

from app.core.settings import get_settings  # noqa: E402
from app.models import ProcessingSession  # noqa: E402
from app.services.analytics import AnalyticsService, Scope  # noqa: E402
from app.services.identity_manager import Observation  # noqa: E402
from app.services.visit_manager import TrackSnap  # noqa: E402
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

LFW = ROOT / "datasets" / "lfw" / "lfw_funneled"
SIZE = (960, 540)
FPS = 25
SECONDS = 5.0


def pick_people(n: int, seed: int) -> dict[str, list[Path]]:
    """n LFW people with at least 3 photographs each (deterministic)."""
    rnd = random.Random(seed)  # noqa: S311
    pool = sorted(d for d in LFW.iterdir() if d.is_dir() and len(list(d.glob("*.jpg"))) >= 3)
    chosen = rnd.sample(pool, n)
    return {d.name: sorted(d.glob("*.jpg"))[:3] for d in chosen}


def make_clip(path: Path, photos: list[Path], mode: str) -> None:
    """One or two faces on a plain background. mode: normal | small | occluded."""
    imgs = [cv2.imread(str(p)) for p in photos]
    w, h = SIZE
    total = int(SECONDS * FPS)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), FPS, SIZE)
    yy, xx = np.mgrid[0:h, 0:w]
    bg = np.stack([40 + 25 * xx / w, 36 + 20 * yy / h, 46 + 0 * xx], axis=-1).astype(np.uint8)
    try:
        for i in range(total):
            t = i / max(1, total - 1)
            frame = bg.copy()
            base = {"normal": 320, "small": 110, "occluded": 320}[mode] if len(imgs) == 1 else 260
            side = int(base + 0.25 * base * t)
            slot = w // len(imgs)
            for k, img in enumerate(imgs):
                tile = cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
                if mode == "occluded":  # mask-like patch over nose and mouth
                    y0, y1 = int(side * 0.52), int(side * 0.88)
                    x0, x1 = int(side * 0.22), int(side * 0.78)
                    tile[y0:y1, x0:x1] = (225, 215, 190)
                cx = int(slot * k + slot * (0.3 + 0.4 * t))
                x0 = int(np.clip(cx - side // 2, 0, w - side))
                frame[(h - side) // 2 : (h - side) // 2 + side, x0 : x0 + side] = tile
            writer.write(frame)
    finally:
        writer.release()


def run_clip(rig: Rig, path: Path, models: Path, n_faces: int) -> dict[str, Any]:
    """Process one clip; returns per-track identity outcome + horizontal position (to tell left from right)."""
    cfg = rig.cfg
    sid = uuid.uuid4().hex
    with rig.factory() as db:
        db.add(
            ProcessingSession(
                id=sid, source_id="gt", source_label="Ground truth", source_type="file", state="RUNNING",
                frames_read=0, frames_processed=0, frames_skipped=0, frames_dropped=0,
                corrupt_frames=0, detections_total=0, max_faces_in_frame=0,
                created_at=datetime.now(UTC) + timedelta(seconds=len(rig.visits._sessions)),
            )
        )  # fmt: skip
        db.commit()
    rig.visits.begin_session(sid, "gt", "Ground truth")
    actions: list[Any] = []
    cx: dict[int, list[float]] = defaultdict(list)
    last_fed = [-10_000]

    def feed(res: Any) -> None:
        for t in res.tracks:
            if t.state == "CONFIRMED" and t.observed:
                cx[t.track_id].append((t.bbox[0] + t.bbox[2]) / 2)
        if res.timestamp_ms - last_fed[0] < 1000 / 15:
            return
        last_fed[0] = res.timestamp_ms
        snaps = [TrackSnap(t.track_id, t.state, t.observed, t.first_seen_ms) for t in res.tracks]
        actions.extend(
            rig.visits.on_frame(sid, res.frame_id, res.timestamp_ms, datetime.now(UTC), snaps)
        )

    def on_face(r: Any) -> None:
        if r.embedding is not None:
            rig.visits.on_observation(sid, r.track_id, r.crop_jpeg, r.quality, r.timestamp_ms)
        for e in rig.identity.observe(
            Observation(sid, r.track_id, r.seq, R.SFACE.key, r.embedding, r.quality, r.reasons,
                        r.timestamp_ms, r.frame_id, r.crop_jpeg)
        ):  # fmt: skip
            if e.visitor_id is not None:
                v, n = e.visitor_id, e.visitor_number
            elif e.state in ("AMBIGUOUS", "REJECTED"):
                v, n = None, None
            else:
                continue
            actions.extend(
                rig.visits.on_identity(sid, e.track_id, v, n, r.timestamp_ms, datetime.now(UTC))
            )

    recognizer = FaceRecognizer(models, cfg.recognition)
    runner = RecognitionRunner(
        recognizer, on_face, RecognitionScheduler(cfg.recognition), cfg.recognition.queue_size
    )
    detector = YoloOnnxFaceDetector(
        model_path(models, YOLOV11N_FACE), intra_op_threads=4, allow_spinning=False
    )
    pipe = ProcessingPipeline(
        FileVideoSource(path), detector,
        PipelineSettings(frame_skip=2, realtime_pacing=False, stream_max_fps=1),
        on_result=lambda res, _j: feed(res), tracker=FaceByteTracker(), recognition=runner,
        recognition_pad=cfg.recognition.landmark_pad,
    )  # fmt: skip
    pipe.run()
    actions.extend(rig.visits.end_session(sid, "end_of_stream", datetime.now(UTC)))
    tracks = {}
    for (s, tid), ts in list(rig.identity._tracks.items()):  # noqa: SLF001
        if s == sid and tid in cx:
            tracks[tid] = {
                "state": ts.state,
                "visitor_id": ts.visitor_id if ts.state in ("RECOGNIZED", "REGISTERED") else None,
                "cx": float(np.mean(cx[tid])),
                "frames": len(cx[tid]),
            }
    rig.identity.end_session(sid)
    for a in actions:
        if a.kind in ("entry", "terminal"):
            rig.recorder.record(a)
    return {"tracks": tracks, "session_id": sid}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--people", type=int, default=14)
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    models = get_settings().models_path
    rnd = random.Random(args.seed)  # noqa: S311
    people = pick_people(args.people, args.seed)
    names = list(people)
    tmp = Path(tempfile.mkdtemp(prefix="gt-unique-", dir=ROOT / "temp"))

    # ---- scenario: (clip label, mode, [(person, photo index)], ...)
    plan: list[tuple[str, list[tuple[str, int]]]] = []
    for n in names[:10]:
        plan.append(("normal", [(n, 0)]))  # first appearance, single person
    for n in names[10:12]:
        plan.append(
            ("normal", [(names[10], 0), (n if n != names[10] else names[11], 0)])
        )  # simultaneous pair
    plan.append(("small", [(names[12], 0)]))
    plan.append(("occluded", [(names[13], 0)]))
    for n in rnd.sample(names[:10], 5):
        plan.append(("normal", [(n, 1)]))  # returning with another photograph
    for n in rnd.sample(names[:10], 2):
        plan.append(("normal", [(n, 2)]))  # and again
    plan.append(("small", [(names[12], 1)]))  # the small-face person returns
    plan.append(("occluded", [(names[13], 1)]))  # the occluded person returns

    rig = Rig(tmp)
    outcome: list[dict[str, Any]] = []
    started = time.perf_counter()
    try:
        for k, (mode, who) in enumerate(plan):
            clip = tmp / f"clip{k:02d}.mp4"
            make_clip(clip, [people[p][i] for p, i in who], mode)
            res = run_clip(rig, clip, models, len(who))
            # left-to-right order of the labelled people == left-to-right order of the tracks
            order = sorted(res["tracks"].items(), key=lambda kv: kv[1]["cx"])
            assigned: list[tuple[str, str | None]] = []  # (true person, visitor id or None)
            if len(who) == 1:
                for _tid, t in order:
                    assigned.append((who[0][0], t["visitor_id"]))
            else:
                halves = [[], []]  # type: ignore[var-annotated]
                for _tid, t in order:
                    halves[0 if t["cx"] < SIZE[0] / 2 else 1].append(t)
                for idx, group in enumerate(halves):
                    for t in group:
                        assigned.append((who[idx][0], t["visitor_id"]))
            outcome.append({"clip": k, "mode": mode, "people": [p for p, _ in who], "photo": [i for _, i in who],
                            "tracks": len(res["tracks"]), "assigned": assigned})  # fmt: skip
            print(
                k,
                mode,
                [p for p, _ in who],
                [a[1][:6] if a[1] else None for a in assigned],
                flush=True,
            )
        wall = time.perf_counter() - started

        # ---- scoring (only because identity labels exist)
        visitor_people: dict[str, Counter[str]] = defaultdict(Counter)
        person_visitors: dict[str, set[str]] = defaultdict(set)
        resolved_by_person: Counter[str] = Counter()
        appearances: Counter[str] = Counter()
        for o in outcome:
            for person in o["people"]:
                appearances[person] += 1
            for person, vid in o["assigned"]:
                if vid:
                    visitor_people[vid][person] += 1
                    person_visitors[person].add(vid)
                    resolved_by_person[person] += 1
        gt_people = sorted({p for o in outcome for p in o["people"]})
        registered = rig.identity.visitor_count()
        merges = [v for v, c in visitor_people.items() if len(c) > 1]
        duplicates = {p: sorted(vs) for p, vs in person_visitors.items() if len(vs) > 1}
        missed = [p for p in gt_people if p not in person_visitors]
        correct = [
            p
            for p in gt_people
            if len(person_visitors.get(p, ())) == 1
            and not any(
                p in visitor_people[v] and len(visitor_people[v]) > 1 for v in person_visitors[p]
            )
        ]
        returning_gt = sorted(p for p, n in appearances.items() if n >= 2)
        svc = AnalyticsService(rig.factory)
        ov = svc.overview(Scope())
        by_mode: dict[str, dict[str, int]] = defaultdict(lambda: {"appearances": 0, "resolved": 0})
        for o in outcome:
            for person in o["people"]:
                by_mode[o["mode"]]["appearances"] += 1
            for _p, vid in o["assigned"]:
                by_mode[o["mode"]]["resolved"] += 1 if vid else 0
        report = {
            "labels": "LFW verified identities (one folder = one person); different photograph per appearance",
            "clips": len(plan),
            "ground_truth_unique_people": len(gt_people),
            "registered_identities": registered,
            "unique_count_absolute_error": abs(registered - len(gt_people)),
            "analytics_unique_visitors": ov["unique_visitors_in_scope"],
            "people_correctly_registered": len(correct),
            "people_missed": missed,
            "duplicate_registrations": duplicates,
            "duplicate_registration_count": sum(len(v) - 1 for v in duplicates.values()),
            "incorrect_merges": [
                {"visitor": v[:8], "people": dict(visitor_people[v])} for v in merges
            ],
            "ground_truth_returning_people": len(returning_gt),
            "analytics_returning_visitors": ov["returning_visitors_in_scope"],
            "visits": ov["visits"],
            "appearances": sum(appearances.values()),
            "appearance_resolution_by_mode": dict(by_mode),
            "tracks_without_identity": sum(
                1 for o in outcome for _p, vid in o["assigned"] if not vid
            ),
            "reconciliation_consistent": svc.reconcile()["consistent"],
            "wall_s": round(wall, 1),
            "caveats": "Still photographs shown full frame; optimistic for real surveillance video. Not valid for the hackathon footage.",
            "per_clip": outcome,
        }
        (args.out / "unique-count-ground-truth.json").write_text(
            json.dumps(report, indent=2, default=str)
        )
        print(
            json.dumps({k: v for k, v in report.items() if k != "per_clip"}, indent=2, default=str)
        )
    finally:
        rig.close()


if __name__ == "__main__":
    main()
