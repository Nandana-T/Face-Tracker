r"""Run the real detection pipeline over real footage and report what it actually did.

    .venv\Scripts\python.exe backend\benchmarks\evaluate_video.py ^
        --videos datasets\hackathon --out datasets\evidence\hackathon [--max-videos N]

Outputs (all under --out, which is git-ignored because the frames show real people):
  results.json            per-video and aggregate metrics (resolution, fps, duration, frames, faces per
                          analysed frame, confidence, inference latency, CPU/RSS)
  frames/*.jpg            annotated evidence frames (green = YOLO11n-face; orange = YuNet cross-check)
  review/*.jpg            contact sheets for manual review

Counts here are DETECTIONS in individual frames. They are not visitors and not unique people.
The second detector (YuNet, MIT licence, models/candidates) is only an independent cross-check used
to point a human at likely false positives / misses - it is not ground truth.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import psutil

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.core.config import load_app_config  # noqa: E402
from app.core.settings import get_settings  # noqa: E402
from app.vision.detection.model_registry import YOLOV11N_FACE, model_path  # noqa: E402
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector  # noqa: E402
from app.vision.sources.file_source import FileVideoSource  # noqa: E402
from app.workers.pipeline import PipelineSettings, ProcessingPipeline  # noqa: E402

YUNET = ROOT / "models" / "candidates" / "yunet" / "face_detection_yunet_2023mar.onnx"


def probe(path: Path) -> dict[str, Any]:
    cap = cv2.VideoCapture(str(path))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
        return {
            "file": path.name,
            "size_mb": round(path.stat().st_size / 2**20, 1),
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(fps, 3),
            "frames_declared": n,
            "duration_s": round(n / fps, 1) if fps else None,
            "codec": "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00"),
        }
    finally:
        cap.release()


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def run_pipeline(
    path: Path, cfg_skip: int, threads: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    detector = YoloOnnxFaceDetector(
        model_path(get_settings().models_path, YOLOV11N_FACE),
        intra_op_threads=threads,
        allow_spinning=False,
    )
    results: list[dict[str, Any]] = []
    pipe = ProcessingPipeline(
        FileVideoSource(path),
        detector,
        PipelineSettings(
            frame_skip=cfg_skip,
            queue_size=4,
            realtime_pacing=False,
            stream_max_fps=15,
            stream_max_width=960,
            jpeg_quality=70,
        ),
        on_result=lambda r, j: results.append(r.model_dump(mode="json")),
    )
    proc = psutil.Process()
    proc.cpu_percent(None)
    rss_peak = 0
    done = threading.Event()

    def sample() -> None:
        nonlocal rss_peak
        while not done.is_set():
            rss_peak = max(rss_peak, proc.memory_info().rss)
            time.sleep(0.1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    t0 = time.perf_counter()
    outcome = pipe.run()
    wall = time.perf_counter() - t0
    done.set()
    cpu = proc.cpu_percent(None) / (psutil.cpu_count(logical=True) or 1)
    s = pipe.stats
    return (
        {
            "outcome": outcome.state,
            "frames_read": s.frames_read,
            "frames_analysed": s.frames_processed,
            "frames_skipped": s.frames_skipped,
            "corrupt_frames": s.corrupt_frames + int(getattr(pipe.source, "corrupt_frames", 0)),
            "wall_s": round(wall, 2),
            "detector_runs_per_s": round(s.frames_processed / wall, 1),
            "source_frames_per_s": round(s.frames_read / wall, 1),
            "avg_inference_ms": round(s._inference_total_ms / max(1, s.frames_processed), 2),  # noqa: SLF001
            "cpu_percent_of_machine": round(cpu, 1),
            "peak_rss_mb": round(rss_peak / 2**20),
        },
        results,
    )


YUNET_WIDTH = (
    960  # YuNet is run on a 960 px-wide copy (YOLO also sees <= 640 px); boxes are scaled back
)


def yunet_boxes(detector: Any, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
    h, w = frame.shape[:2]
    scale = min(1.0, YUNET_WIDTH / w)
    small = (
        cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
        if scale < 1
        else frame
    )
    detector.setInputSize((small.shape[1], small.shape[0]))
    _, faces = detector.detect(small)
    if faces is None:
        return []
    k = 1.0 / scale
    return [
        (
            float(f[0]) * k,
            float(f[1]) * k,
            float(f[0] + f[2]) * k,
            float(f[1] + f[3]) * k,
            float(f[14]),
        )
        for f in faces
    ]


def draw(
    frame: np.ndarray, yolo: list[dict[str, Any]], yunet: list[tuple[float, ...]], label: str
) -> np.ndarray:
    out = frame.copy()
    t = max(2, round(out.shape[1] / 640))
    for f in yunet:
        cv2.rectangle(
            out, (int(f[0]), int(f[1])), (int(f[2]), int(f[3])), (0, 140, 255), max(1, t - 1)
        )
    for f in yolo:
        x1, y1, x2, y2 = f["bbox"]
        cv2.rectangle(out, (x1, y1), (x2, y2), (0, 220, 0), t)
        cv2.putText(
            out,
            f"{f['confidence']:.2f}",
            (x1, max(14, y1 - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5 * t,
            (0, 255, 0),
            max(1, t - 1),
            cv2.LINE_AA,
        )
    cv2.putText(
        out, label, (10, 24 * t), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * t, (255, 255, 255), t, cv2.LINE_AA
    )
    return out


def read_frame(path: Path, index: int) -> np.ndarray | None:
    cap = cv2.VideoCapture(str(path))
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, index)
        ok, frame = cap.read()
        return frame if ok else None
    finally:
        cap.release()


def downscale(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    return (
        img
        if w <= width
        else cv2.resize(img, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--videos", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-videos", type=int, default=0)
    ap.add_argument("--frame-skip", type=int, default=None)
    args = ap.parse_args()

    cfg = load_app_config(get_settings().config_path)
    skip = cfg.detection.frame_skip if args.frame_skip is None else args.frame_skip
    threads = cfg.detection.intra_op_threads
    files = sorted(
        p for p in args.videos.glob("*") if p.suffix.lower() in (".mp4", ".avi", ".mkv", ".mov")
    )
    if args.max_videos:
        files = files[: args.max_videos]
    (args.out / "frames").mkdir(parents=True, exist_ok=True)
    (args.out / "review").mkdir(parents=True, exist_ok=True)
    yunet = (
        cv2.FaceDetectorYN.create(str(YUNET), "", (320, 320), 0.5, 0.3, 5000)
        if YUNET.is_file()
        else None
    )

    per_video: list[dict[str, Any]] = []
    rng = random.Random(7)
    evidence: list[tuple[str, Path, int, list[dict[str, Any]], list[tuple[float, ...]], str]] = []
    for path in files:
        print(f"{path.name} ...", flush=True)
        info = probe(path)
        run, results = run_pipeline(path, skip, threads)
        faces = [len(r["faces"]) for r in results]
        confs = [f["confidence"] for r in results for f in r["faces"]]
        sizes = [
            min(f["bbox"][2] - f["bbox"][0], f["bbox"][3] - f["bbox"][1])
            for r in results
            for f in r["faces"]
        ]
        dist: dict[str, int] = {}
        for n in faces:
            key = str(n) if n < 5 else "5+"
            dist[key] = dist.get(key, 0) + 1
        # --- independent cross-check on exactly the same analysed frames -----------------------
        agree = {"matched": 0, "yolo_only": 0, "yunet_only": 0}
        disagreements: list[int] = []
        yunet_by_frame: dict[int, list[tuple[float, ...]]] = {}
        if yunet is not None:
            cap = cv2.VideoCapture(str(path))
            want = {r["frame_id"] for r in results}
            by_id = {r["frame_id"]: r for r in results}
            idx = 0
            while want:
                ok, frame = cap.read()
                if not ok:
                    break
                if idx in want:
                    want.discard(idx)
                    yb = yunet_boxes(yunet, frame)
                    yunet_by_frame[idx] = yb  # type: ignore[assignment]
                    yolo = [tuple(f["bbox"]) for f in by_id[idx]["faces"]]
                    used: set[int] = set()
                    m = 0
                    for box in yolo:
                        best = max(
                            (
                                (iou(box, tuple(b[:4])), j)
                                for j, b in enumerate(yb)
                                if j not in used
                            ),
                            default=(0.0, -1),
                        )
                        if best[0] >= 0.3:
                            used.add(best[1])
                            m += 1
                    agree["matched"] += m
                    agree["yolo_only"] += len(yolo) - m
                    agree["yunet_only"] += len(yb) - m
                    if len(yolo) != m or len(yb) != m:
                        disagreements.append(idx)
                idx += 1
            cap.release()
        per_video.append(
            {
                **info,
                **run,
                "faces_per_analysed_frame": {
                    "mean": round(statistics.fmean(faces), 3) if faces else 0,
                    "max": max(faces, default=0),
                    "distribution": dist,
                },
                "confidence": {
                    "mean": round(statistics.fmean(confs), 3) if confs else None,
                    "p05": round(float(np.percentile(confs, 5)), 3) if confs else None,
                    "min": round(min(confs), 3) if confs else None,
                },
                "smallest_face_px": min(sizes, default=None),
                "yolo_detections": sum(faces),
                "cross_check_vs_yunet": agree if yunet else "yunet model not present",
                "disagreement_frames": len(disagreements),
            }
        )
        # evidence candidates: most faces, a disagreement, a random middle frame
        if results:
            most = max(results, key=lambda r: len(r["faces"]))
            picks = [("most-faces", most["frame_id"])]
            if disagreements:
                picks.append(("disagreement", rng.choice(disagreements)))  # noqa: S311
            picks.append(("sample", rng.choice(results)["frame_id"]))
            for tag, fid in picks:
                r = next(x for x in results if x["frame_id"] == fid)
                evidence.append(
                    (path.stem, path, fid, r["faces"], yunet_by_frame.get(fid, []), tag)
                )
        # frames with the lowest-confidence detection (likely false-positive suspects)
        low = sorted(
            (f["confidence"], r["frame_id"], r["faces"]) for r in results for f in r["faces"]
        )[:1]
        for conf, fid, fl in low:
            evidence.append(
                (path.stem, path, fid, fl, yunet_by_frame.get(fid, []), f"lowest-conf-{conf:.2f}")
            )

    # ---- write evidence frames + contact sheets ------------------------------------------------
    saved: list[Path] = []
    for stem, path, fid, yolo_faces, yb, tag in evidence:
        frame = read_frame(path, fid)
        if frame is None:
            continue
        img = downscale(draw(frame, yolo_faces, yb, f"{stem[-6:]} #{fid} {tag}"), 1280)
        dest = args.out / "frames" / f"{stem}_f{fid}_{tag}.jpg"
        cv2.imwrite(str(dest), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        saved.append(dest)
    for k in range(0, len(saved), 6):
        tiles = [downscale(cv2.imread(str(p)), 640) for p in saved[k : k + 6]]
        h = max(t.shape[0] for t in tiles)
        tiles = [cv2.copyMakeBorder(t, 0, h - t.shape[0], 0, 0, cv2.BORDER_CONSTANT) for t in tiles]
        while len(tiles) % 2:
            tiles.append(np.zeros_like(tiles[0]))
        rows = [np.hstack(tiles[i : i + 2]) for i in range(0, len(tiles), 2)]
        cv2.imwrite(
            str(args.out / "review" / f"sheet_{k // 6:02d}.jpg"),
            np.vstack(rows),
            [cv2.IMWRITE_JPEG_QUALITY, 80],
        )

    total_frames = sum(v["frames_read"] for v in per_video)
    total_wall = sum(v["wall_s"] for v in per_video)
    agg = {
        "videos": len(per_video),
        "total_duration_s": round(sum(v["duration_s"] or 0 for v in per_video), 1),
        "frames_read": total_frames,
        "frames_analysed": sum(v["frames_analysed"] for v in per_video),
        "corrupt_frames": sum(v["corrupt_frames"] for v in per_video),
        "yolo_detections": sum(v["yolo_detections"] for v in per_video),
        "wall_s": round(total_wall, 1),
        "detector_runs_per_s": round(sum(v["frames_analysed"] for v in per_video) / total_wall, 1)
        if total_wall
        else 0,
        "avg_inference_ms": round(statistics.fmean(v["avg_inference_ms"] for v in per_video), 2)
        if per_video
        else 0,
        "mean_cpu_percent_of_machine": round(
            statistics.fmean(v["cpu_percent_of_machine"] for v in per_video), 1
        )
        if per_video
        else 0,
        "max_peak_rss_mb": max((v["peak_rss_mb"] for v in per_video), default=0),
        "settings": {
            "frame_skip": skip,
            "intra_op_threads": threads,
            "cpu_spin_wait": False,
            "confidence_threshold": cfg.detection.confidence_threshold,
        },
    }
    (args.out / "results.json").write_text(
        json.dumps({"aggregate": agg, "videos": per_video}, indent=2), encoding="utf-8"
    )
    print(json.dumps(agg, indent=2))
    print(f"evidence frames: {len(saved)} in {args.out / 'frames'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
