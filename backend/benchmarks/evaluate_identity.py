r"""Identity-level evaluation: duplicate registrations and wrong identity merges (labelled LFW identities).

    .venv\Scripts\python.exe backend\benchmarks\evaluate_identity.py --out datasets\evidence\recognition [--people 300]

Protocol (decision logic exactly as in production - the real IdentityManager, thresholds from
config/config.json, a throw-away SQLite database):
  Session 1: every selected LFW person appears once, as ONE track made of several observations of one
             photo (small jitter: scale, shift, brightness, blur; each one goes through the real
             YOLO -> YuNet -> align -> SFace pipeline).   Expected: each person registers once.
  Session 2: every person appears again as a track built from a DIFFERENT photo of the same person.
             Expected: recognised as the visitor registered in session 1 - no new registration.
Measured: registrations, wrong merges (a track recognised as a different person), duplicates (a known
person registered again), and how often the system declines to decide (pending/ambiguous/rejected).

Limits: LFW photos are posed press photos; frames of one video track are MORE alike than the
jittered copies used here (optimistic for registration consistency) while a different photo of the same
person (session 2) varies MORE than a video frame (pessimistic for recognition). Persons are known
distinct individuals; this is NOT an evaluation on surveillance footage.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import cv2
import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import load_app_config  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import ProcessingSession  # noqa: E402
from app.services.identity_manager import IdentityManager, Observation  # noqa: E402
from app.services.image_store import LocalImageStore  # noqa: E402
from app.vision.detection.model_registry import YOLOV11N_FACE  # noqa: E402
from app.vision.detection.model_registry import model_path as det_path  # noqa: E402
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector  # noqa: E402
from app.vision.recognition import registry as R  # noqa: E402
from app.vision.recognition.alignment import align_face  # noqa: E402
from app.vision.recognition.embedder import OrtEmbedder  # noqa: E402
from app.vision.recognition.landmarks import YuNetLandmarks  # noqa: E402
from app.vision.recognition.quality import assess  # noqa: E402
from app.vision.recognition.service import quality_config  # noqa: E402
from app.vision.sources.interfaces import Frame  # noqa: E402

IMAGES = ROOT / "datasets" / "lfw" / "lfw_funneled"


def jitter(img: np.ndarray, rnd: random.Random) -> np.ndarray:
    h, w = img.shape[:2]
    scale = rnd.uniform(0.95, 1.05)
    dx, dy = rnd.uniform(-3, 3), rnd.uniform(-3, 3)
    m = np.array(
        [[scale, 0, dx + (1 - scale) * w / 2], [0, scale, dy + (1 - scale) * h / 2]], np.float32
    )
    out = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
    out = np.clip(
        out.astype(np.float32) * rnd.uniform(0.9, 1.1) + rnd.uniform(-8, 8), 0, 255
    ).astype(np.uint8)
    if rnd.random() < 0.5:
        out = cv2.GaussianBlur(out, (3, 3), 0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--people", type=int, default=300)
    ap.add_argument("--obs", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--sweep",
        action="store_true",
        help="evaluate several threshold settings on the SAME cached observations",
    )
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    cfg = load_app_config(ROOT / "config" / "config.json").recognition
    models = get_settings().models_path
    detector = YoloOnnxFaceDetector(
        det_path(models, YOLOV11N_FACE), intra_op_threads=4, allow_spinning=False
    )
    landmarks = YuNetLandmarks(R.model_path(models, R.YUNET), pad=cfg.landmark_pad)
    embedder = OrtEmbedder(R.model_path(models, R.SFACE), R.SFACE, threads=4)
    qcfg = quality_config(cfg)

    people = {d.name: sorted(d.glob("*.jpg")) for d in IMAGES.iterdir() if d.is_dir()}
    eligible = sorted(n for n, files in people.items() if len(files) >= 4)
    rnd.shuffle(eligible)
    chosen = eligible[: args.people]

    def observe(img: np.ndarray) -> tuple[np.ndarray | None, float]:
        faces = detector.detect(Frame(0, 0, img))
        if not faces:
            return None, 0.0
        h, w = img.shape[:2]
        f = min(
            faces, key=lambda f: abs((f.x1 + f.x2) / 2 - w / 2) + abs((f.y1 + f.y2) / 2 - h / 2)
        )
        box = (f.x1, f.y1, f.x2, f.y2)
        lm = landmarks.locate(img, box)
        if lm is None:
            return None, 0.0
        aligned = align_face(img, lm.points)
        q = assess(qcfg, box, f.confidence, (w, h), lm.points, aligned)
        # LFW is cropped tight: do not penalise the "cut by frame edge" check, it is a video concept.
        reasons = tuple(r for r in q.reasons if r != "cut_by_frame_edge")
        if reasons:
            return None, q.score
        return embedder.embed(aligned), q.score

    cache_file = args.out / f"identity-observations-{args.people}-{args.obs}-{args.seed}.npz"
    tracks: dict[str, dict[str, list[tuple[np.ndarray | None, float]]]] = {"A": {}, "B": {}}
    if cache_file.exists():
        raw = np.load(cache_file, allow_pickle=True)
        tracks = raw["tracks"].item()
    else:
        for name in chosen:
            files = people[name]
            for which, photo in (("A", files[0]), ("B", rnd.choice(files[1:]))):
                img = cv2.imread(str(photo))
                tracks[which][name] = [
                    observe(img if k == 0 else jitter(img, rnd)) for k in range(args.obs)
                ]
        np.savez(cache_file, tracks=np.array(tracks, dtype=object))

    def run_config(overrides: dict[str, Any]) -> dict[str, Any]:
        cfg2 = cfg.model_copy(update=overrides)
        tmp = Path(tempfile.mkdtemp(prefix="identity-eval-", dir=ROOT / "temp"))
        engine = create_engine(f"sqlite:///{tmp / 'eval.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        mgr = IdentityManager(
            factory, LocalImageStore(tmp / "faces"), cfg2, R.SFACE.key, R.SFACE.embedding_dim
        )
        mgr.load()
        order_rnd = random.Random(args.seed + 1)

        def run_session(sid: str, which: str) -> dict[str, dict[str, Any]]:
            with factory() as db:
                db.add(
                    ProcessingSession(
                        id=sid,
                        source_id="x",
                        source_label="LFW",
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
            order = chosen[:]
            order_rnd.shuffle(order)
            clock = 0
            out: dict[str, dict[str, Any]] = {}
            for track, name in enumerate(order, start=1):
                for k, (emb, quality) in enumerate(tracks[which][name]):
                    clock += 400
                    mgr.observe(
                        Observation(
                            sid,
                            track,
                            k,
                            R.SFACE.key,
                            emb,
                            quality if emb is not None else 0.0,
                            () if emb is not None else ("quality",),
                            clock,
                            k,
                            None,
                        )
                    )
                ts = mgr._tracks[(sid, track)]  # noqa: SLF001
                out[name] = {"state": ts.state, "visitor": ts.visitor_id}
            return out

        s1 = run_session("a" * 32, "A")
        owner: dict[str, str] = {}
        c1: Counter[str] = Counter()
        for name, r in s1.items():
            if r["state"] == "REGISTERED":
                owner[r["visitor"]] = name
                c1["registered"] += 1
            elif r["state"] == "RECOGNIZED":
                c1["wrong_merge" if owner.get(r["visitor"]) != name else "ok"] += 1
            else:
                c1[r["state"].lower()] += 1
        registered_people = set(owner.values())
        s2 = run_session("b" * 32, "B")
        c2: Counter[str] = Counter()
        for name, r in s2.items():
            if r["state"] == "RECOGNIZED":
                c2[
                    "recognised_correctly" if owner.get(r["visitor"]) == name else "WRONG_IDENTITY"
                ] += 1
            elif r["state"] == "REGISTERED":
                # a person who already owns a visitor and gets a second one = a duplicate registration
                c2[
                    "DUPLICATE_registration" if name in registered_people else "first_registration"
                ] += 1
            else:
                c2[r["state"].lower()] += 1
        n = len(chosen)
        returned_known = sum(1 for name in s2 if name in registered_people)
        rec_ok = c2["recognised_correctly"]
        return {
            "overrides": overrides,
            "session1": dict(c1),
            "session2": dict(c2),
            "rates": {
                "s1_registered_of_people": round(c1["registered"] / n, 3),
                "s1_undecided": round(
                    (
                        c1["match_pending"]
                        + c1["ambiguous"]
                        + c1["rejected"]
                        + c1["unresolved"]
                        + c1["insufficient_quality"]
                    )
                    / n,
                    3,
                ),
                "s1_wrong_merge": round(c1["wrong_merge"] / n, 4),
                "s2_known_people_recognised": round(rec_ok / max(1, returned_known), 3),
                "s2_WRONG_IDENTITY": round(c2["WRONG_IDENTITY"] / n, 4),
                "s2_DUPLICATE_of_known": round(
                    c2["DUPLICATE_registration"] / max(1, returned_known), 4
                ),
            },
            "visitors_in_database": mgr.visitor_count(),
            "avg_lookup_ms": mgr.runtime_stats()["avg_lookup_ms"],
            "avg_registration_ms": mgr.runtime_stats()["avg_registration_ms"],
        }

    configs: list[dict[str, Any]] = [{}]
    if args.sweep:
        configs = [
            {"novelty_threshold": nov, "similarity_threshold": sim}
            for nov in (0.30, 0.35, 0.40)
            for sim in (0.40, 0.45, 0.50)
            if nov <= sim
        ]
    results = [run_config(c) for c in configs]
    report = {"people": len(chosen), "observations_per_track": args.obs, "results": results}
    (args.out / "identity-simulation.json").write_text(json.dumps(report, indent=2))
    for r in results:
        print(
            json.dumps(
                {"overrides": r["overrides"], **r["rates"], "visitors": r["visitors_in_database"]}
            )
        )


if __name__ == "__main__":
    main()
