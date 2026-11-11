r"""Recognition threshold evaluation on the LFW verification protocol (labelled identities).

    .venv\Scripts\python.exe backend\benchmarks\evaluate_recognition.py embed  --out datasets\evidence\recognition [--folds N] [--pad 0.6]
    .venv\Scripts\python.exe backend\benchmarks\evaluate_recognition.py report --out datasets\evidence\recognition

Data: Labeled Faces in the Wild (funneled, ``pairs.txt``: 10 folds x 300 genuine + 300 impostor
pairs) in ``datasets/lfw`` (git-ignored; research benchmark of public-figure press photos, used only
locally for evaluation, never redistributed or used to build the visitor database).

Every image goes through the SAME code path as production: YOLO11n-face box -> YuNet landmarks on a
padded crop -> similarity alignment -> SFace embedding. Images where any step fails are counted as
failures-to-process, never silently dropped from the statistics.

What this does and does not tell you: genuine/impostor similarity distributions and error rates for
*this* model on *LFW-quality* images (mostly frontal, good light, 250x250). Surveillance footage is
harder, so these numbers are optimistic bounds, not expected field accuracy.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.core.settings import get_settings  # noqa: E402
from app.vision.detection.model_registry import YOLOV11N_FACE  # noqa: E402
from app.vision.detection.model_registry import model_path as det_path  # noqa: E402
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector  # noqa: E402
from app.vision.recognition import registry as R  # noqa: E402
from app.vision.recognition.alignment import align_face  # noqa: E402
from app.vision.recognition.embedder import OrtEmbedder  # noqa: E402
from app.vision.recognition.landmarks import YuNetLandmarks  # noqa: E402
from app.vision.recognition.quality import sharpness  # noqa: E402
from app.vision.sources.interfaces import Frame  # noqa: E402

LFW = ROOT / "datasets" / "lfw"
IMAGES = LFW / "lfw_funneled"


def read_pairs(folds: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    lines = (LFW / "pairs.txt").read_text().splitlines()[1:]
    genuine: list[tuple[str, str]] = []
    impostor: list[tuple[str, str]] = []
    per_fold = 600
    for fold in range(folds):
        chunk = lines[fold * per_fold : (fold + 1) * per_fold]
        for line in chunk[:300]:
            name, i, j = line.split("\t")
            genuine.append((f"{name}/{name}_{int(i):04d}.jpg", f"{name}/{name}_{int(j):04d}.jpg"))
        for line in chunk[300:]:
            n1, i, n2, j = line.split("\t")
            impostor.append((f"{n1}/{n1}_{int(i):04d}.jpg", f"{n2}/{n2}_{int(j):04d}.jpg"))
    return genuine, impostor


def cmd_embed(out: Path, folds: int, pad: float, scale: float = 1.0, blur: float = 0.0) -> None:
    out.mkdir(parents=True, exist_ok=True)
    genuine, impostor = read_pairs(folds)
    names = sorted({n for p in genuine + impostor for n in p})
    detector = YoloOnnxFaceDetector(
        det_path(get_settings().models_path, YOLOV11N_FACE),
        intra_op_threads=4,
        allow_spinning=False,
    )
    models = get_settings().models_path
    landmarks = YuNetLandmarks(R.model_path(models, R.YUNET), pad=pad)
    embedder = OrtEmbedder(R.model_path(models, R.SFACE), R.SFACE, threads=4)
    vectors: dict[str, list[float]] = {}
    failures: dict[str, str] = {}
    meta: dict[str, dict[str, float]] = {}
    t0 = time.perf_counter()
    for k, name in enumerate(names):
        pixels = cv2.imread(str(IMAGES / name))
        if pixels is None:
            failures[name] = "unreadable"
            continue
        if (
            scale != 1.0
        ):  # simulate a smaller / more distant face (what surveillance footage looks like)
            pixels = cv2.resize(pixels, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if blur > 0:  # defocus-like blur, to find where recognition quality really degrades
            pixels = cv2.GaussianBlur(pixels, (0, 0), blur)
        frame = Frame(0, 0, pixels)
        faces = detector.detect(frame)
        if not faces:
            failures[name] = "no_face"
            continue
        # LFW images are centred on one face: take the detection nearest the centre, then the largest.
        h, w = pixels.shape[:2]
        face = min(
            faces,
            key=lambda f: abs((f.x1 + f.x2) / 2 - w / 2) + abs((f.y1 + f.y2) / 2 - h / 2),
        )
        lm = landmarks.locate(pixels, (face.x1, face.y1, face.x2, face.y2))
        if lm is None:
            failures[name] = "no_landmarks"
            continue
        aligned = align_face(pixels, lm.points)
        vectors[name] = embedder.embed(aligned).tolist()
        meta[name] = {
            "face_w": round(float(face.x2 - face.x1), 1),
            "sharp": round(sharpness(aligned), 1),
        }
        if k % 500 == 0:
            print(f"{k}/{len(names)} {time.perf_counter() - t0:.0f}s", flush=True)
    tag = (
        f"pad{pad}"
        + (f"-scale{scale}" if scale != 1.0 else "")
        + (f"-blur{blur}" if blur > 0 else "")
    )
    (out / f"lfw-embeddings-{tag}.json").write_text(
        json.dumps(
            {
                "pad": pad,
                "folds": folds,
                "scale": scale,
                "vectors": vectors,
                "failures": failures,
                "meta": meta,
            }
        )
    )
    print(f"done: {len(vectors)} embedded, {len(failures)} failed, {time.perf_counter() - t0:.0f}s")


def cmd_report(out: Path, tag: str) -> None:
    data = json.loads((out / f"lfw-embeddings-{tag}.json").read_text())
    folds = data["folds"]
    vec = {k: np.asarray(v, dtype=np.float32) for k, v in data["vectors"].items()}
    genuine, impostor = read_pairs(folds)

    def sims(pairs: list[tuple[str, str]]) -> tuple[np.ndarray, int]:
        ok = [(a, b) for a, b in pairs if a in vec and b in vec]
        return np.array([float(vec[a] @ vec[b]) for a, b in ok]), len(pairs) - len(ok)

    g, g_fail = sims(genuine)
    i, i_fail = sims(impostor)
    thresholds = np.round(np.arange(0.0, 0.95, 0.01), 2)
    table = []
    for t in thresholds:
        fmr = float((i >= t).mean())
        fnmr = float((g < t).mean())
        table.append((float(t), fmr, fnmr))
    # Pairwise accuracy at the best single threshold (the standard LFW number, for reference only).
    best_acc = max(
        (1 - (fmr * len(i) + fnmr * len(g)) / (len(i) + len(g)), t) for t, fmr, fnmr in table
    )

    def at_fmr(target: float) -> dict[str, float]:
        for t, fmr, fnmr in table:
            if fmr <= target:
                return {"threshold": t, "fmr": fmr, "fnmr": fnmr}
        return {"threshold": 1.0, "fmr": 0.0, "fnmr": 1.0}

    def at_fnmr(target: float) -> dict[str, float]:
        best = table[0]
        for row in table:
            if row[2] <= target:
                best = row
        return {"threshold": best[0], "fmr": best[1], "fnmr": best[2]}

    pct = lambda a, qs: {str(q): round(float(np.percentile(a, q)), 3) for q in qs}  # noqa: E731
    report = {
        "model": "sface-2021dec",
        "dataset": "LFW (funneled) verification pairs",
        "folds": folds,
        "pad": data["pad"],
        "images_embedded": len(vec),
        "images_failed": len(data["failures"]),
        "failure_reasons": {
            r: sum(1 for v in data["failures"].values() if v == r)
            for r in set(data["failures"].values())
        },
        "genuine_pairs_scored": len(g),
        "genuine_pairs_failed_to_process": g_fail,
        "impostor_pairs_scored": len(i),
        "impostor_pairs_failed_to_process": i_fail,
        "genuine_similarity": {"mean": round(float(g.mean()), 3), **pct(g, [1, 5, 25, 50, 75, 95])},
        "impostor_similarity": {
            "mean": round(float(i.mean()), 3),
            **pct(i, [5, 25, 50, 75, 95, 99, 99.9]),
        },
        "max_impostor_similarity": round(float(i.max()), 3),
        "best_single_threshold_accuracy": {
            "accuracy": round(best_acc[0], 4),
            "threshold": best_acc[1],
        },
        "operating_points": {
            "FMR<=1e-1": at_fmr(1e-1),
            "FMR<=1e-2": at_fmr(1e-2),
            "FMR<=1e-3": at_fmr(1e-3),
            "FMR=0 (no impostor pair above)": at_fmr(0.0),
            "FNMR<=1%": at_fnmr(0.01),
            "FNMR<=2%": at_fnmr(0.02),
            "FNMR<=5%": at_fnmr(0.05),
        },
        "table": [{"t": t, "fmr": round(a, 5), "fnmr": round(b, 5)} for t, a, b in table[::5]],
        "caveats": [
            "LFW pairs are mostly frontal press photos; surveillance footage is harder, so error rates here are optimistic.",
            f"FMR below ~1/n_impostor_pairs cannot be resolved (here n = {len(i)}).",
        ],
    }
    (out / f"recognition-lfw-{tag}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "table"}, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["embed", "report"])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--folds", type=int, default=10)
    ap.add_argument("--pad", type=float, default=0.6)
    ap.add_argument(
        "--scale", type=float, default=1.0, help="downscale LFW images to simulate small faces"
    )
    ap.add_argument(
        "--blur", type=float, default=0.0, help="Gaussian blur sigma applied after scaling"
    )
    args = ap.parse_args()
    if args.command == "embed":
        cmd_embed(args.out, args.folds, args.pad, args.scale, args.blur)
    else:
        cmd_report(
            args.out, f"pad{args.pad}" + (f"-scale{args.scale}" if args.scale != 1.0 else "")
        )


if __name__ == "__main__":
    main()
