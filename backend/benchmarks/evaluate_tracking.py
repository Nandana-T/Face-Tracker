r"""Phase 2 tracking evaluation on the real hackathon footage.

    .venv\Scripts\python.exe backend\benchmarks\evaluate_tracking.py detect  --out datasets\evidence\tracking
    .venv\Scripts\python.exe backend\benchmarks\evaluate_tracking.py track   --out datasets\evidence\tracking
    .venv\Scripts\python.exe backend\benchmarks\evaluate_tracking.py ab      --out datasets\evidence\tracking
    .venv\Scripts\python.exe backend\benchmarks\evaluate_tracking.py frames  --out datasets\evidence\tracking

* detect : runs the REAL YOLO11n-face detector on every frame (frame_skip=0) of a representative
           subset and caches the raw detections (detector output does not depend on frame_skip, so
           skip=2 / skip=5 are exact subsamples of the cache).
* track  : replays the cache through the tracker for frame_skip 0/2/5 and reports track statistics.
* ab     : A/B on the real pipeline (with vs without tracker): CPU time, RSS, wall time.
* frames : renders annotated evidence frames + review strips (real people: stays git-ignored).

There is NO ground truth for this footage. Everything printed here is either a direct measurement
(timings, counts) or a PROXY (e.g. "suspected fragmentations": a new track that starts near where an
older one ended). Identity-switch metrics (IDF1/MOTA/HOTA) are only computed on the synthetic
benchmark with known trajectories (synthetic_tracking_benchmark.py) - never on this footage.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
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
from app.vision.detection.interfaces import FaceDetection  # noqa: E402
from app.vision.detection.model_registry import YOLOV11N_FACE, model_path  # noqa: E402
from app.vision.detection.yolo_onnx import YoloOnnxFaceDetector  # noqa: E402
from app.vision.sources.file_source import FileVideoSource  # noqa: E402
from app.vision.tracking.interfaces import TrackState  # noqa: E402
from app.vision.tracking.tracker import FaceByteTracker, TrackerConfig  # noqa: E402
from app.workers.pipeline import PipelineSettings, ProcessingPipeline  # noqa: E402

FOOTAGE = ROOT / "datasets" / "hackathon"
SUBSET = {
    "record_20250620_183903.mp4": "multi-person (up to 3), moderate motion",
    "record_20250620_184807.mp4": "multi-person (up to 5)",
    "record_20250620_185210.mp4": "multi-person, entering/leaving",
    "record_20250620_184403.mp4": "2-3 people, partial visibility",
    "record_20250620_184605.mp4": "sparse: mostly single, small faces",
    "record_20250620_190324.mp4": "two people, crossing candidates",
    "video_sample1.mp4": "crowded 4K (up to 20 faces)",
}
SKIPS = (0, 2, 5)


def tracker_config() -> TrackerConfig:
    t = load_app_config(ROOT / "config" / "config.json").tracking
    return TrackerConfig(
        high_confidence=t.high_confidence,
        confirm_hits=t.confirm_hits,
        tentative_timeout_seconds=t.tentative_timeout_seconds,
        max_missing_seconds=t.max_missing_seconds,
        match_iou=t.match_iou,
        low_match_iou=t.low_match_iou,
        lost_match_iou=t.lost_match_iou,
        box_buffer=t.box_buffer,
        lost_velocity_decay_per_second=t.lost_velocity_decay_per_second,
        max_tracks=t.max_tracks,
    )


def make_detector() -> YoloOnnxFaceDetector:
    return YoloOnnxFaceDetector(
        model_path(get_settings().models_path, YOLOV11N_FACE),
        intra_op_threads=4,
        allow_spinning=False,
    )


# ------------------------------------------------------------------------------ detect
def cmd_detect(out: Path) -> None:
    cache = out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    detector = make_detector()
    for name in SUBSET:
        target = cache / f"{name}.json"
        if target.exists():
            print("cached", name)
            continue
        source = FileVideoSource(FOOTAGE / name)
        info = source.open()
        rows: list[dict[str, Any]] = []
        t0 = time.perf_counter()
        for frame in source.frames():
            faces = detector.detect(frame)
            rows.append(
                {
                    "i": frame.index,
                    "t": frame.timestamp_ms,
                    "f": [
                        [round(f.x1, 1), round(f.y1, 1), round(f.x2, 1), round(f.y2, 1)]
                        + [round(f.confidence, 4)]
                        for f in faces
                    ],
                }
            )
        source.close()
        target.write_text(
            json.dumps(
                {"w": info.width, "h": info.height, "fps": info.fps, "frames": rows},
                separators=(",", ":"),
            )
        )
        print(f"{name}: {len(rows)} frames in {time.perf_counter() - t0:.0f}s", flush=True)


# ------------------------------------------------------------------------------ track
def load_cache(out: Path, name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((out / "cache" / f"{name}.json").read_text())
    return data


def dets_of(row: dict[str, Any]) -> list[FaceDetection]:
    return [FaceDetection(*f) for f in row["f"]]


def replay(
    data: dict[str, Any], skip: int, cfg: TrackerConfig
) -> tuple[list[Any], list[float], FaceByteTracker]:
    tracker = FaceByteTracker(cfg)
    outputs: list[Any] = []
    timings: list[float] = []
    size = (data["w"], data["h"])
    for row in data["frames"]:
        if row["i"] % (skip + 1):
            continue
        started = time.perf_counter()
        out = tracker.update(dets_of(row), row["t"], row["i"], size)
        timings.append((time.perf_counter() - started) * 1000)
        outputs.append(out)
    return outputs, timings, tracker


def histories(outputs: list[Any]) -> dict[int, dict[str, Any]]:
    h: dict[int, dict[str, Any]] = {}
    for out in outputs:
        for t in out.tracks:
            e = h.setdefault(
                t.track_id,
                {
                    "first_ms": t.first_seen_ms,
                    "first_box": t.bbox,
                    "last_obs_box": t.bbox,
                    "last_ms": t.last_seen_ms,
                    "confirmed": False,
                    "lost_updates": 0,
                    "updates": 0,
                    "observed_updates": 0,
                },
            )
            e["updates"] += 1
            e["last_ms"] = t.last_seen_ms
            if t.observed:
                e["last_obs_box"] = t.bbox
                e["observed_updates"] += 1
            if t.state is TrackState.CONFIRMED:
                e["confirmed"] = True
            if t.state is TrackState.LOST:
                e["lost_updates"] += 1
    return h


def suspected_fragmentations(hist: dict[int, dict[str, Any]], window_s: float = 3.0) -> int:
    """PROXY: confirmed tracks whose end is followed, within `window_s`, by a new track starting
    within 1.5 face-widths of where they were last observed. No ground truth - an upper-bound hint."""
    count = 0
    confirmed = {k: v for k, v in hist.items() if v["confirmed"]}
    for a_id, a in confirmed.items():
        ax1, ay1, ax2, ay2 = a["last_obs_box"]
        acx, acy, aw = (ax1 + ax2) / 2, (ay1 + ay2) / 2, ax2 - ax1
        for b_id, b in confirmed.items():
            if b_id <= a_id:
                continue
            gap = (b["first_ms"] - a["last_ms"]) / 1000
            if not (0 < gap <= window_s):
                continue
            bx1, by1, bx2, by2 = b["first_box"]
            if np.hypot((bx1 + bx2) / 2 - acx, (by1 + by2) / 2 - acy) <= 1.5 * max(aw, 1):
                count += 1
                break
    return count


def cmd_track(out: Path) -> None:
    cfg = tracker_config()
    report: dict[str, Any] = {"config": cfg.__dict__, "videos": {}, "subset": SUBSET}
    for name, desc in SUBSET.items():
        data = load_cache(out, name)
        per_skip: dict[str, Any] = {}
        for skip in SKIPS:
            outputs, timings, tracker = replay(data, skip, cfg)
            hist = histories(outputs)
            counts = tracker.counts()
            confirmed = [v for v in hist.values() if v["confirmed"]]
            durations = [(v["last_ms"] - v["first_ms"]) / 1000 for v in confirmed]
            raw = sum(len(r["f"]) for r in data["frames"] if r["i"] % (skip + 1) == 0)
            per_skip[str(skip)] = {
                "analysed_frames": len(outputs),
                "raw_detections": raw,
                "tracks_created": counts.created_total,
                "tracks_confirmed": counts.confirmed_total,
                "tracks_rejected_tentative": counts.rejected_total,
                "confirmed_tracks_expired": counts.removed_total,
                "recoveries": counts.recovered_total,
                "median_confirmed_duration_s": round(statistics.median(durations), 2)
                if durations
                else None,
                "max_confirmed_duration_s": round(max(durations), 2) if durations else None,
                "confirmed_shorter_than_1s": sum(1 for d in durations if d < 1.0),
                "suspected_fragmentations_proxy": suspected_fragmentations(hist),
                "tracking_ms_mean": round(statistics.fmean(timings), 4) if timings else 0,
                "tracking_ms_p99": round(float(np.percentile(timings, 99)), 4) if timings else 0,
                "max_simultaneous_tracks": max((len(o.tracks) for o in outputs), default=0),
            }
        report["videos"][name] = {"description": desc, **per_skip}
        print(name, json.dumps(per_skip["2"]), flush=True)
    (out / "tracking-results.json").write_text(json.dumps(report, indent=2))


# ------------------------------------------------------------------------------ A/B
def run_pipeline(
    name: str, with_tracker: bool, max_frames: int, cfg: TrackerConfig
) -> dict[str, Any]:
    source = FileVideoSource(FOOTAGE / name)
    detector = make_detector()
    pipe = ProcessingPipeline(
        source,
        detector,
        PipelineSettings(frame_skip=2, realtime_pacing=False, stream_max_fps=15),
        on_result=lambda r, j: None,
        tracker=FaceByteTracker(cfg) if with_tracker else None,
    )
    orig = source.frames

    def limited() -> Any:
        for k, frame in enumerate(orig()):
            if k >= max_frames:
                return
            yield frame

    source.frames = limited  # type: ignore[method-assign]
    proc = psutil.Process()
    cpu0 = proc.cpu_times()
    rss_peak = 0
    t0 = time.perf_counter()
    import threading

    stop = threading.Event()

    def sampler() -> None:
        nonlocal rss_peak
        while not stop.is_set():
            rss_peak = max(rss_peak, proc.memory_info().rss)
            time.sleep(0.05)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()
    pipe.run()
    wall = time.perf_counter() - t0
    stop.set()
    cpu1 = proc.cpu_times()
    st = pipe.stats
    return {
        "wall_s": round(wall, 2),
        "cpu_s": round((cpu1.user + cpu1.system) - (cpu0.user + cpu0.system), 2),
        "analysed": st.frames_processed,
        "peak_rss_mb": round(rss_peak / 2**20),
        "avg_inference_ms": round(st._inference_total_ms / max(1, st.frames_processed), 2),  # noqa: SLF001
        "tracking_ms_avg": round(st._tracking_total_ms / max(1, st.frames_processed), 4),  # noqa: SLF001
    }


def cmd_ab(out: Path) -> None:
    cfg = tracker_config()
    results: dict[str, Any] = {}
    for name in ("record_20250620_183903.mp4", "video_sample1.mp4"):
        runs: dict[str, list[dict[str, Any]]] = {"without": [], "with": []}
        for rep in range(3):
            for label, flag in (("without", False), ("with", True)):
                r = run_pipeline(name, flag, 600, cfg)
                runs[label].append(r)
                print(name, rep, label, r, flush=True)
        summary: dict[str, Any] = {}
        for label, rs in runs.items():
            summary[label] = {
                k: round(statistics.median(x[k] for x in rs), 3)
                for k in ("wall_s", "cpu_s", "peak_rss_mb", "avg_inference_ms", "tracking_ms_avg")
            }
        summary["runs"] = runs
        results[name] = summary
    (out / "ab-results.json").write_text(json.dumps(results, indent=2))


# ------------------------------------------------------------------------------ frames
COLORS = {
    TrackState.CONFIRMED: (80, 200, 60),
    TrackState.TENTATIVE: (0, 215, 255),
    TrackState.LOST: (170, 170, 170),
}


def annotate(frame: np.ndarray, out: Any, scale: float) -> np.ndarray:
    img = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    for t in out.tracks:
        x1, y1, x2, y2 = (int(v * scale) for v in t.bbox)
        color = COLORS[t.state]
        label = f"#{t.track_id}" + ("" if t.observed else " pred")
        if t.observed:
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        else:  # dashed look for predicted boxes
            for xa in range(x1, x2, 10):
                cv2.line(img, (xa, y1), (min(xa + 5, x2), y1), color, 2)
                cv2.line(img, (xa, y2), (min(xa + 5, x2), y2), color, 2)
            for ya in range(y1, y2, 10):
                cv2.line(img, (x1, ya), (x1, min(ya + 5, y2)), color, 2)
                cv2.line(img, (x2, ya), (x2, min(ya + 5, y2)), color, 2)
        cv2.putText(img, label, (x1, max(14, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)
    return img


def cmd_frames(out: Path) -> None:
    cfg = tracker_config()
    dest = out / "frames"
    dest.mkdir(parents=True, exist_ok=True)
    for name in SUBSET:
        data = load_cache(out, name)
        outputs, _, _ = replay(data, 2, cfg)
        by_frame = {o.frame_id: o for o in outputs}
        # interesting moments: most tracks at once, a LOST->recovered, two confirmed tracks close
        picks: list[int] = []
        busiest = max(outputs, key=lambda o: len(o.tracks), default=None)
        if busiest:
            picks.append(busiest.frame_id)
        for o in outputs:
            if any(e.kind == "recovered" for e in o.events):
                picks.append(o.frame_id)
                break
        for o in outputs:
            lost = [t for t in o.tracks if t.state is TrackState.LOST]
            if lost:
                picks.append(o.frame_id)
                break
        picks += [outputs[len(outputs) // 4].frame_id, outputs[len(outputs) // 2].frame_id]
        cap = cv2.VideoCapture(str(FOOTAGE / name))
        scale = 1280 / data["w"]
        for idx in sorted(set(picks)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if ok and idx in by_frame:
                cv2.imwrite(
                    str(dest / f"{Path(name).stem}_f{idx:05d}.jpg"),
                    annotate(frame, by_frame[idx], scale),
                    [cv2.IMWRITE_JPEG_QUALITY, 85],
                )
        # review strip: 6 consecutive analysed frames around the busiest moment
        if busiest:
            ids = [o.frame_id for o in outputs]
            k = ids.index(busiest.frame_id)
            window = ids[max(0, k - 3) : k + 3]
            tiles = []
            for idx in window:
                cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame = cap.read()
                if ok:
                    tiles.append(annotate(frame, by_frame[idx], 640 / data["w"]))
            if len(tiles) >= 2:
                rows = [
                    np.hstack(tiles[i : i + 3])
                    for i in range(0, len(tiles) - len(tiles) % 3 or 3, 3)
                ]
                width = max(r.shape[1] for r in rows)
                rows = [
                    np.pad(r, ((0, 0), (0, width - r.shape[1]), (0, 0))) for r in rows if r.size
                ]
                cv2.imwrite(str(dest / f"{Path(name).stem}_strip.jpg"), np.vstack(rows))
        cap.release()
        print("frames", name, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["detect", "track", "ab", "frames"])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    {"detect": cmd_detect, "track": cmd_track, "ab": cmd_ab, "frames": cmd_frames}[args.command](
        args.out
    )


if __name__ == "__main__":
    main()
