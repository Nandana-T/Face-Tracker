r"""Reproducible Phase 1 benchmark (real model, real video, nothing simulated).

    .venv\Scripts\python.exe backend\benchmarks\benchmark_detector.py --out docs\benchmarks

Writes ``phase1-benchmark.json`` and prints a Markdown summary. Sections:
  1. system        CPU model, cores, RAM, GPU / execution providers
  2. model         file size, input size, load time
  3. latency       per-stage latency (preprocess / inference / postprocess) for several ORT
                   thread settings and several source frame sizes
  4. pipeline      end-to-end throughput for several frame_skip values on the 40 s sample video,
                   CPU and memory utilisation
  5. delivery      worker -> API -> browser-socket latency through the live stack

The sample video is synthetic (public-domain portrait panned/zoomed); latency and throughput do
not depend on image content for a fixed-size CNN, but detection accuracy is NOT measured here.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort
import psutil

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.core.settings import get_settings  # noqa: E402
from app.vision.detection.model_registry import YOLOV11N_FACE, model_path  # noqa: E402
from app.vision.detection.yolo_onnx import (  # noqa: E402
    YoloOnnxFaceDetector,
    decode_detections,
    letterbox,
    to_tensor,
)
from app.vision.sample_video import build_sample_video  # noqa: E402
from app.vision.sources.file_source import FileVideoSource  # noqa: E402
from app.vision.sources.interfaces import Frame  # noqa: E402
from app.workers.pipeline import PipelineSettings, ProcessingPipeline  # noqa: E402


def pct(values: list[float], q: float) -> float:
    return float(np.percentile(values, q))


def stats(values: list[float]) -> dict[str, float]:
    return {
        "mean": round(statistics.fmean(values), 2),
        "p50": round(pct(values, 50), 2),
        "p95": round(pct(values, 95), 2),
        "p99": round(pct(values, 99), 2),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
    }


def cpu_name() -> str:
    if sys.platform == "win32":
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        ) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def gpus() -> list[str]:
    names: list[str] = []
    try:
        out = subprocess.run(  # noqa: S603
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController).Name -join '|'",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout.strip()
        names = [n for n in out.split("|") if n]
    except (OSError, subprocess.SubprocessError):
        pass
    nvidia = (
        subprocess.run(  # noqa: S603
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
        )
        if _has("nvidia-smi")
        else None
    )
    if nvidia and nvidia.returncode == 0:
        names.append("NVIDIA: " + nvidia.stdout.strip())
    return names


def _has(cmd: str) -> bool:
    from shutil import which

    return which(cmd) is not None


def system_info() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    return {
        "cpu": cpu_name(),
        "physical_cores": psutil.cpu_count(logical=False),
        "logical_cores": psutil.cpu_count(logical=True),
        "ram_gb": round(vm.total / 2**30, 1),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "python": platform.python_version(),
        "onnxruntime": ort.__version__,
        "opencv": cv2.__version__,
        "ort_providers_available": ort.get_available_providers(),
        "gpus_reported_by_os": gpus(),
        "cuda_device_usable": "CUDAExecutionProvider" in ort.get_available_providers(),
    }


def load_times(path: Path, repeats: int = 7) -> dict[str, float]:
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        d = YoloOnnxFaceDetector(path)
        times.append((time.perf_counter() - started) * 1000)
        d.close()
    return stats(times)


def sample_frame(width: int, height: int) -> np.ndarray:
    """A real face frame at the requested size (the portrait on a plain background)."""
    img = cv2.imread(str(BACKEND / "tests" / "fixtures" / "astronaut_nasa_public_domain.png"))
    canvas = np.full((height, width, 3), (46, 40, 36), dtype=np.uint8)
    side = min(width, height) - 20
    tile = cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
    x0, y0 = (width - side) // 2, (height - side) // 2
    canvas[y0 : y0 + side, x0 : x0 + side] = tile
    return canvas


def stage_latency(
    path: Path, threads: int, size: tuple[int, int], iterations: int
) -> dict[str, Any]:
    detector = YoloOnnxFaceDetector(path, intra_op_threads=threads)
    frame = sample_frame(*size)
    session = detector._session  # noqa: SLF001 - benchmark reaches in to time stages separately
    name = detector._input_name  # noqa: SLF001
    for _ in range(15):  # warm-up (allocator, caches, thread pool)
        detector.detect(Frame(0, 0, frame))
    pre, inf, post, total = [], [], [], []
    proc = psutil.Process()
    proc.cpu_percent(None)
    wall0 = time.perf_counter()
    for _ in range(iterations):
        t0 = time.perf_counter()
        square, scale, pad = letterbox(frame, 640)
        tensor = to_tensor(square)
        t1 = time.perf_counter()
        out = session.run(None, {name: tensor})[0]
        t2 = time.perf_counter()
        decode_detections(
            out,
            conf_threshold=0.5,
            iou_threshold=0.45,
            scale=scale,
            pad=pad,
            frame_width=size[0],
            frame_height=size[1],
            max_faces=100,
        )
        t3 = time.perf_counter()
        pre.append((t1 - t0) * 1000)
        inf.append((t2 - t1) * 1000)
        post.append((t3 - t2) * 1000)
        total.append((t3 - t0) * 1000)
    wall = time.perf_counter() - wall0
    cpu = proc.cpu_percent(None)  # % of ONE logical core (can exceed 100)
    detector.close()
    return {
        "threads": threads or "auto",
        "frame_size": f"{size[0]}x{size[1]}",
        "iterations": iterations,
        "preprocess_ms": stats(pre),
        "inference_ms": stats(inf),
        "postprocess_ms": stats(post),
        "total_ms": stats(total),
        "effective_fps": round(iterations / wall, 1),
        "process_cpu_percent_of_one_core": round(cpu, 0),
        "process_cpu_percent_of_machine": round(cpu / (psutil.cpu_count(logical=True) or 1), 1),
    }


def pipeline_run(
    path: Path,
    video: Path,
    frame_skip: int,
    threads: int,
    paced: bool = False,
    spinning: bool = True,
) -> dict[str, Any]:
    source = FileVideoSource(video)
    detector = YoloOnnxFaceDetector(path, intra_op_threads=threads, allow_spinning=spinning)
    pipe = ProcessingPipeline(
        source,
        detector,
        PipelineSettings(
            frame_skip=frame_skip,
            queue_size=4,
            realtime_pacing=paced,
            stream_max_fps=15,
            stream_max_width=960,
            jpeg_quality=70,
        ),
        on_result=lambda r, j: None,
    )
    proc = psutil.Process()
    proc.cpu_percent(None)
    rss_peak = 0

    def sample() -> None:
        nonlocal rss_peak
        while not done.is_set():
            rss_peak = max(rss_peak, proc.memory_info().rss)
            time.sleep(0.1)

    done = threading.Event()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    outcome = pipe.run()
    wall = time.perf_counter() - started
    done.set()
    cpu = proc.cpu_percent(None)
    s = pipe.stats
    return {
        "frame_skip": frame_skip,
        "intra_op_threads": threads or "auto",
        "realtime_paced": paced,
        "ort_spin_wait": spinning,
        "outcome": outcome.state,
        "frames_read": s.frames_read,
        "frames_processed": s.frames_processed,
        "wall_seconds": round(wall, 2),
        "source_frames_per_second_consumed": round(s.frames_read / wall, 1),
        "detector_fps": round(s.frames_processed / wall, 1),
        "avg_inference_ms": round(s._inference_total_ms / max(1, s.frames_processed), 2),  # noqa: SLF001
        "process_cpu_percent_of_machine": round(cpu / (psutil.cpu_count(logical=True) or 1), 1),
        "peak_rss_mb": round(rss_peak / 2**20, 0),
        "realtime_factor_vs_25fps": round((s.frames_read / wall) / 25.0, 2),
    }


def delivery_latency(seconds: float = 20.0) -> dict[str, Any]:
    """Worker publish -> API relay -> browser WebSocket receive, on this host (same clock)."""
    from sqlalchemy import create_engine
    from websockets.sync.client import connect

    from app.db.base import Base
    from app.workers import protocol as P
    from tests.integration.test_live_pipeline import Stack

    tmp = Path(tempfile.mkdtemp(prefix="katomaran-bench-"))
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp / 's.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    stack = Stack(tmp, engine)
    stack.cfg.worker.realtime_pacing = True
    stack.start()
    latencies: list[float] = []
    try:
        sid = stack.start_session("long")["id"]
        stack.wait_state(sid, ("RUNNING",))
        end = time.monotonic() + seconds
        with connect(
            f"ws://127.0.0.1:{stack.port}/api/v1/sessions/{sid}/stream", max_size=P.MAX_FRAME_BYTES
        ) as ws:
            while time.monotonic() < end:
                try:
                    msg = ws.recv(timeout=3)
                except TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    header, _ = P.unpack_frame(msg)
                    if "published_at_ms" in header:
                        latencies.append(time.time() * 1000 - header["published_at_ms"])
        assert stack.client is not None
        stack.client.post(f"/sessions/{sid}/stop")
    finally:
        stack.stop()
    return {
        "frames_measured": len(latencies),
        "latency_ms": stats(latencies) if latencies else None,
        "note": "worker publish -> API relay -> WebSocket client, same host, loopback",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "benchmarks")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument(
        "--paced-only",
        action="store_true",
        help="only (re)measure real-time-paced runs and merge into the existing JSON",
    )
    args = parser.parse_args()

    settings = get_settings()
    path = model_path(settings.models_path, YOLOV11N_FACE)
    if not path.is_file():
        print("Model missing: run scripts\\download-model.ps1", file=sys.stderr)
        return 1
    video = ROOT / "datasets" / "e2e" / "long.mp4"
    if not video.is_file():
        build_sample_video(video, seconds=40, fps=25, faces=1)

    if args.paced_only:
        result = json.loads((args.out / "phase1-benchmark.json").read_text(encoding="utf-8"))
        print("paced runs ...", flush=True)
        result["pipeline_realtime"] = [
            pipeline_run(path, video, 2, t, paced=True, spinning=sp)
            for t in (0, 4)
            for sp in (True, False)
        ]
        (args.out / "phase1-benchmark.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(json.dumps(result["pipeline_realtime"], indent=2))
        return 0

    result: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "system": system_info(),
    }
    result["model"] = {
        "name": YOLOV11N_FACE.key,
        "file_mb": round(path.stat().st_size / 2**20, 2),
        "input": "1x3x640x640 float32 (letterboxed)",
        "sha256": YOLOV11N_FACE.sha256,
        "load_ms": load_times(path),
    }
    print("latency matrix ...", flush=True)
    result["latency"] = [
        stage_latency(path, t, (960, 540), args.iterations) for t in (1, 2, 4, 8, 0)
    ] + [
        stage_latency(path, 0, size, args.iterations)
        for size in ((640, 360), (1280, 720), (1920, 1080))
    ]
    print("pipeline ...", flush=True)
    result["pipeline"] = [pipeline_run(path, video, k, 0) for k in (0, 2, 5)]
    print("paced runs ...", flush=True)
    result["pipeline_realtime"] = [
        pipeline_run(path, video, 2, t, paced=True, spinning=sp)
        for t in (0, 4)
        for sp in (True, False)
    ]
    print("delivery ...", flush=True)
    result["delivery"] = delivery_latency()

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "phase1-benchmark.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
