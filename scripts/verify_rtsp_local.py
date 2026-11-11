r"""RTSP integration against a LOCAL RTSP server (MediaMTX) fed by ffmpeg from a sample video.

    .venv\Scripts\python.exe scripts\verify_rtsp_local.py --out datasets\evidence\rtsp-local

SCOPE - read this: this is a LOCAL SERVER INTEGRATION test. A real RTSP server and a real RTSP client path
(OpenCV/FFmpeg demuxer over TCP) are exercised on loopback, with a looped sample video. It is NOT a test of a
physical network camera (no camera firmware, codec quirks, Wi-Fi loss, NAT, credentials on a real device).
Do not describe it as real-camera verification.

Tools (not committed, kept on D: under .tools/rtsp): MediaMTX (MIT) and an ffmpeg build from the
`imageio-ffmpeg` wheel. Verifies: connection, decoding, detection, tracking, recognition (a visitor is
registered), stream interruption (publisher killed), automatic reconnection and continued processing,
a clean stop and resource cleanup (worker threads/handles back to idle), credential redaction in the API.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOOLS = ROOT / ".tools" / "rtsp"
API_PORT = 8011
BASE = f"http://127.0.0.1:{API_PORT}/api/v1"
RTSP_URL = "rtsp://127.0.0.1:8554/live"
WORKER_TOKEN = "rtsp-test-worker-token-0123456789-abc"


def ffmpeg_exe() -> str:
    sys.path.insert(0, str(TOOLS / "pkgs"))
    import imageio_ffmpeg

    return str(imageio_ffmpeg.get_ffmpeg_exe())


def kill(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        parent = psutil.Process(proc.pid)
        family = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for p in family:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(family, timeout=8)


def wait_for(fn: Any, timeout: float, what: str) -> Any:
    end = time.time() + timeout
    last: Exception | None = None
    while time.time() < end:
        try:
            r = fn()
            if r:
                return r
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.4)
    raise TimeoutError(f"timed out waiting for {what} ({last})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    import pgserver

    url = pgserver.get_server(ROOT / "storage" / "pgdata", cleanup_mode=None).get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    env0 = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env0, check=True, capture_output=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "e2e_reset_identity.py")], cwd=ROOT, env=env0, check=True, capture_output=True)

    cfg = json.loads((ROOT / "config" / "config.e2e.json").read_text(encoding="utf-8"))
    cfg["video"]["sources"].append({"id": "rtsp-local", "label": "Local RTSP (MediaMTX)", "type": "rtsp", "url_env": "RTSP_LOCAL"})
    cfg["video"]["rtsp"] = {"connect_timeout_seconds": 5, "read_timeout_seconds": 4, "max_reconnect_attempts": 0, "max_backoff_seconds": 4}
    cfg["worker"]["realtime_pacing"] = False  # an RTSP source is live anyway
    (ROOT / "temp").mkdir(exist_ok=True)
    (ROOT / "temp" / "config.rtsp.json").write_text(json.dumps(cfg), encoding="utf-8")
    env = {**env0, "CONFIG_FILE": "temp/config.rtsp.json", "KATOMARAN_ROOT": str(ROOT), "WORKER_TOKEN": WORKER_TOKEN,
           "RTSP_LOCAL": RTSP_URL, "WORKER_API_URL": f"ws://127.0.0.1:{API_PORT}"}  # fmt: skip
    worker_env = {k: v for k, v in env.items() if k != "DATABASE_URL"}

    procs: dict[str, subprocess.Popen[bytes] | None] = {"mtx": None, "pub": None, "api": None, "worker": None}
    steps: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}
    client = httpx.Client(base_url=BASE, timeout=15)

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

    def publish() -> None:
        procs["pub"] = subprocess.Popen(
            [ffmpeg_exe(), "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", str(ROOT / "datasets" / "e2e" / "long.mp4"),
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", "25",
             "-f", "rtsp", "-rtsp_transport", "tcp", RTSP_URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip

    def session(sid: str) -> dict[str, Any]:
        return client.get(f"/sessions/{sid}").json()  # type: ignore[no-any-return]

    try:
        procs["mtx"] = subprocess.Popen([str(TOOLS / "mediamtx" / "mediamtx.exe"), str(TOOLS / "mediamtx" / "mediamtx.yml")], cwd=TOOLS / "mediamtx", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        publish()
        time.sleep(3)
        step("RTSP server (MediaMTX) and publisher (ffmpeg, looped sample video) are up", procs["pub"] is not None and procs["pub"].poll() is None)
        procs["api"] = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app_factory", "--factory", "--host", "127.0.0.1", "--port", str(API_PORT)], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(lambda: client.get("/health").status_code == 200, 40, "API")
        procs["worker"] = subprocess.Popen([sys.executable, "-m", "app.workers.video_worker"], cwd=BACKEND, env=worker_env, stdout=(ROOT / "temp" / "rtsp-worker.log").open("wb"), stderr=subprocess.STDOUT)
        wait_for(lambda: client.get("/sources").json()["worker"]["connected"], 60, "worker")
        src = next(s for s in client.get("/sources").json()["sources"] if s["id"] == "rtsp-local")
        step("the RTSP source is offered and its URL never appears in the API", src["availability"] == "available" and "rtsp://" not in json.dumps(client.get("/sources").json()))
        wpid = psutil.Process(procs["worker"].pid).children(recursive=True)  # type: ignore[union-attr]
        wproc = max([psutil.Process(procs["worker"].pid), *wpid], key=lambda p: p.memory_info().rss)  # type: ignore[union-attr]
        idle_threads = wproc.num_threads()

        t = time.time()
        sid = client.post("/sessions", json={"source_id": "rtsp-local"}).json()["id"]
        wait_for(lambda: session(sid)["state"] == "RUNNING", 40, "RUNNING over RTSP")
        metrics["rtsp_connect_to_running_s"] = round(time.time() - t, 2)
        step("connection and decoding: the session reaches RUNNING over RTSP", True, f"{metrics['rtsp_connect_to_running_s']}s")
        wait_for(lambda: session(sid)["stats"]["frames_processed"] > 30, 40, "frames")
        s = wait_for(lambda: (x := session(sid))["stats"]["tracks_confirmed_total"] >= 1 and x, 40, "tracking")
        step("detection and tracking on the RTSP frames", s["stats"]["detections_total"] > 0 and s["stats"]["tracks_confirmed_total"] >= 1, f"{s['stats']['frames_processed']} frames analysed")
        wait_for(lambda: client.get("/analytics/overview").json()["registered_visitors"] >= 1, 60, "registration")
        step("recognition: a visitor is registered from the RTSP stream", True)
        wait_for(lambda: client.get("/analytics/overview").json()["active_visits"] >= 1, 30, "active visit")

        # interruption: the publisher dies -> reconnecting; then comes back
        t = time.time()
        kill(procs["pub"])
        try:
            s = wait_for(lambda: (x := session(sid))["connection"] == "reconnecting" and x, 40, "reconnecting")
        except TimeoutError:
            x = session(sid)
            print("DEBUG after interruption:", x["state"], x["connection"], x["stats"]["frames_processed"], "publisher alive:", procs["pub"].poll() is None if procs["pub"] else None, flush=True)  # type: ignore[union-attr]
            raise
        metrics["interruption_to_reconnecting_s"] = round(time.time() - t, 1)
        step("stream interruption is detected and reported as reconnecting", s["state"] == "RUNNING", f"{metrics['interruption_to_reconnecting_s']}s")
        frames_before = session(sid)["stats"]["frames_processed"]
        time.sleep(3)
        step("the session stays alive (no crash, no fake frames) while the source is down", session(sid)["state"] == "RUNNING" and session(sid)["stats"]["frames_processed"] - frames_before <= 5)
        t = time.time()
        publish()
        wait_for(lambda: session(sid)["connection"] == "connected" and session(sid)["stats"]["frames_processed"] > frames_before + 10, 60, "reconnect")
        metrics["restore_to_processing_s"] = round(time.time() - t, 1)
        step("automatic reconnection: processing resumes after the source returns", True, f"{metrics['restore_to_processing_s']}s")
        ov = client.get("/analytics/overview").json()
        step("no duplicate identity or visit storm caused by the interruption", ov["registered_visitors"] == 1)

        # clean stop and cleanup
        client.post(f"/sessions/{sid}/stop")
        wait_for(lambda: session(sid)["state"] == "STOPPED", 30, "STOPPED")
        time.sleep(3)
        threads_after = wproc.num_threads()
        metrics["worker_threads_idle_vs_after_stop"] = [idle_threads, threads_after]
        metrics["worker_rss_mb"] = round(wproc.memory_info().rss / 2**20)
        step("clean stop and resource cleanup (worker threads back to idle)", threads_after <= idle_threads + 1, f"{idle_threads} -> {threads_after}")
        step("open visits closed (none counted as present after the stop)", client.get("/analytics/overview").json()["active_visits"] == 0)
        rec = client.get("/analytics/reconciliation").json()
        step("reconciliation consistent", rec["consistent"])
    except Exception as exc:  # noqa: BLE001
        step("harness error", False, f"{type(exc).__name__}: {exc}")
    finally:
        for name in ("worker", "api", "pub", "mtx"):
            kill(procs[name])
    report = {
        "scope": "LOCAL RTSP SERVER integration (MediaMTX + ffmpeg, loopback). NOT a physical network camera test.",
        "steps": steps, "metrics": metrics, "passed": sum(s["ok"] for s in steps), "failed": sum(not s["ok"] for s in steps),
    }  # fmt: skip
    (args.out / "rtsp-local.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"passed": report["passed"], "failed": report["failed"], "metrics": metrics}, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
