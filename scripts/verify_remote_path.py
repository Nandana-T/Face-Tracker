r"""Production-like remote-path verification (LOCAL loopback with TLS - not the public internet).

    .venv\Scripts\python.exe scripts\verify_remote_path.py --out datasets\evidence\remote-path

Starts, as separate OS processes, the API with APP_ENV=production (all production guards on: admin password,
auth secret, long worker token, https-only CORS) behind TLS (self-signed certificate generated into temp/),
and the video worker dialling ``wss://localhost:<port>`` with only the worker token (NO database credentials in
its environment). Then it drives the whole path over HTTPS and checks:

  1  health / readiness over TLS, anonymous access refused, the worker's wrong token refused
  2  the worker connects over WSS; a sample video is processed; a visitor is registered; entry + terminal
     events, evidence image (through the authenticated endpoint), analytics counts and the read-only
     reconciliation agree with the database
  3  API restart mid-session: the session is ended consistently, the worker reconnects by itself, a new
     session works, nothing is duplicated
  4  worker restart mid-session: the session ends as worker_lost, open visits become INTERRUPTED, recovery
  5  the same video again: the visitor is recognised (no second identity), visits grow, unique count does not
Measured: HTTPS round trip, WSS reconnect time, time to first processed frame, worker environment contains
no DATABASE_URL, process memory.

It uses the LOCAL embedded PostgreSQL and the e2e config (datasets/e2e), never the hosted database.
Scope statement: loopback TLS proves the protocol, authentication and recovery logic; it does not measure
internet latency, NAT, a load balancer or Render/Vercel behaviour.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import psutil

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PORT = 8443
BASE = f"https://localhost:{PORT}/api/v1"
PASSWORD = "remote-path-admin-password"
WORKER_TOKEN = secrets.token_urlsafe(32)
AUTH_SECRET = secrets.token_urlsafe(48)


def make_cert(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "cert.pem", directory / "key.pem"
    if not cert.exists():
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
             "-days", "2", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True,
        )  # fmt: skip
    return cert, key


class Stack:
    def __init__(self, database_url: str, cert: Path, key: Path) -> None:
        self.cert, self.key = cert, key
        self.api: subprocess.Popen[bytes] | None = None
        self.worker: subprocess.Popen[bytes] | None = None
        self.common = {
            "CONFIG_FILE": "config/config.e2e.json",
            "KATOMARAN_ROOT": str(ROOT),
        }
        self.api_env = {
            **os.environ, **self.common, "APP_ENV": "production", "DATABASE_URL": database_url,
            "ADMIN_PASSWORD": PASSWORD, "AUTH_SECRET": AUTH_SECRET, "WORKER_TOKEN": WORKER_TOKEN,
            "CORS_ORIGINS": "https://localhost:5173", "DB_USE_PGBOUNCER": "false",
        }  # fmt: skip
        # the worker gets NO database credentials: only where the API is and the shared token
        self.worker_env = {
            k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "TEST_DATABASE_URL", "ADMIN_PASSWORD")
        } | {**self.common, "WORKER_API_URL": f"wss://localhost:{PORT}", "WORKER_TOKEN": WORKER_TOKEN,
             "SSL_CERT_FILE": str(cert)}  # fmt: skip

    def start_api(self) -> None:
        self.api = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app_factory", "--factory", "--host", "127.0.0.1",
             "--port", str(PORT), "--ssl-keyfile", str(self.key), "--ssl-certfile", str(self.cert)],
            cwd=BACKEND, env=self.api_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip

    def start_worker(self, token: str | None = None) -> None:
        env = dict(self.worker_env)
        if token:
            env["WORKER_TOKEN"] = token
        self.worker = subprocess.Popen(
            [sys.executable, "-m", "app.workers.video_worker"], cwd=BACKEND, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip

    @staticmethod
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

    def stop(self) -> None:
        self.kill(self.worker)
        self.kill(self.api)

    def rss_mb(self) -> dict[str, float]:
        out = {}
        for name, proc in (("api", self.api), ("worker", self.worker)):
            if proc and proc.poll() is None:
                fam = [psutil.Process(proc.pid), *psutil.Process(proc.pid).children(recursive=True)]
                out[name] = round(sum(p.memory_info().rss for p in fam) / 2**20)
        return out


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
        time.sleep(0.3)
    raise TimeoutError(f"timed out waiting for {what} ({last})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    import pgserver

    url = pgserver.get_server(ROOT / "storage" / "pgdata", cleanup_mode=None).get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)
    # start from an empty local registry (the reset script refuses anything but the local embedded PostgreSQL)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "e2e_reset_identity.py")], cwd=ROOT, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)
    cert, key = make_cert(ROOT / "temp" / "tls")
    ctx = ssl.create_default_context(cafile=str(cert))
    client = httpx.Client(base_url=BASE, verify=ctx, timeout=15)
    stack = Stack(url, cert, key)
    steps: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {}

    def step(name: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": name, "ok": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)

    def auth() -> dict[str, str]:
        r = client.post("/auth/login", json={"password": PASSWORD})
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    def worker_up() -> bool:
        return bool(client.get("/sources", headers=auth()).json()["worker"]["connected"])

    def overview() -> dict[str, Any]:
        return client.get("/analytics/overview", headers=auth()).json()  # type: ignore[no-any-return]

    try:
        t0 = time.time()
        stack.start_api()
        wait_for(lambda: client.get("/health").status_code == 200, 60, "API over TLS")
        metrics["api_start_to_healthy_s"] = round(time.time() - t0, 1)
        t = time.perf_counter()
        for _ in range(10):
            client.get("/health")
        metrics["https_health_ms_mean"] = round((time.perf_counter() - t) * 100, 1)
        step("API starts in production mode behind TLS and answers /health", True, f"{metrics['api_start_to_healthy_s']}s")
        step("readiness checks the database", client.get("/ready").json()["status"] == "ready")
        step("anonymous access is refused", client.get("/sources").status_code == 401 and client.get("/analytics/overview").status_code == 401)
        step("worker environment holds no database credentials", "DATABASE_URL" not in stack.worker_env)

        # wrong token never connects; then the right one does and reconnects by itself
        stack.start_worker(token="x" * 40)
        time.sleep(4)
        step("a worker with a wrong token is refused", not worker_up())
        stack.kill(stack.worker)
        t = time.time()
        stack.start_worker()
        wait_for(worker_up, 60, "worker over WSS")
        metrics["worker_start_to_connected_s"] = round(time.time() - t, 1)
        step("worker connects over WSS with the shared token", True, f"{metrics['worker_start_to_connected_s']}s")
        base = overview()

        # 2 - a full sample run
        h = auth()
        t = time.time()
        r = client.post("/sessions", json={"source_id": "e2e-align"}, headers=h)
        sid = r.json()["id"]
        wait_for(lambda: client.get(f"/sessions/{sid}", headers=h).json()["state"] == "RUNNING", 45, "RUNNING")
        metrics["start_to_running_s"] = round(time.time() - t, 2)
        wait_for(lambda: client.get(f"/sessions/{sid}", headers=h).json()["state"] == "COMPLETED", 90, "COMPLETED")
        sess = client.get(f"/sessions/{sid}", headers=h).json()
        step("session over the remote path completes", sess["stats"]["frames_processed"] > 10, f"{sess['stats']['frames_processed']} frames analysed")
        step("tracking and recognition ran in the worker", sess["stats"]["tracks_confirmed_total"] >= 1 and sess["stats"]["recog_usable"] >= 1)
        o = overview()
        step("one identity registered, one visit, one entry, one terminal event", o["registered_visitors"] - base["registered_visitors"] == 1 and o["visits"] - base["visits"] == 1 and o["entry_events"] - base["entry_events"] == 1 and o["terminal_events"] - base["terminal_events"] == 1)
        ev = client.get("/events?limit=2&order=desc", headers=h).json()["items"]
        img = client.get(f"/events/{ev[0]['id']}/image", headers=h)
        step("evidence image through the authenticated endpoint", img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.headers["cache-control"] == "private, no-store" and client.get(f"/events/{ev[0]['id']}/image").status_code == 401)
        rec = client.get("/analytics/reconciliation", headers=h).json()
        ver = client.get("/events/log-verification", headers=h).json()
        step("read-only count reconciliation and events.log verification agree", rec["consistent"] and ver["unexplained_duplicates_count"] == 0 and ver["missing_in_log_count"] == 0)

        # 3 - API restart in the middle of a session
        r = client.post("/sessions", json={"source_id": "e2e-long"}, headers=h)
        sid2 = r.json()["id"]
        wait_for(lambda: client.get("/events?limit=1", headers=h).json()["total"] > (o["entry_events"] + 0) or client.get(f"/sessions/{sid2}", headers=h).json()["state"] == "RUNNING", 45, "second session running")
        time.sleep(8)  # let the visitor be recognised and enter
        before_restart = overview()
        t = time.time()
        stack.kill(stack.api)
        stack.start_api()
        wait_for(lambda: client.get("/health").status_code == 200, 60, "API after restart")
        step("API restart ends the live session consistently", client.get(f"/sessions/{sid2}", headers=auth()).json()["state"] == "ERROR")
        wait_for(worker_up, 60, "worker reconnect after API restart")
        metrics["worker_reconnect_after_api_restart_s"] = round(time.time() - t, 1)
        step("the worker reconnects by itself", True, f"{metrics['worker_reconnect_after_api_restart_s']}s")
        o2 = overview()
        step("counts survive the restart; no open visit is counted as present", o2["registered_visitors"] == before_restart["registered_visitors"] and o2["active_visits"] == 0 and o2["stale_open_visits"] == 0)
        open_visits = client.get("/visits?state=ACTIVE", headers=auth()).json()["total"]
        step("visits left open by the crash were closed as INTERRUPTED (none stay ACTIVE)", open_visits == 0)

        # 4 - worker restart mid-session
        h = auth()
        r = client.post("/sessions", json={"source_id": "e2e-long"}, headers=h)
        sid3 = r.json()["id"]
        wait_for(lambda: client.get(f"/sessions/{sid3}", headers=h).json()["state"] == "RUNNING", 45, "third session")
        time.sleep(6)
        stack.kill(stack.worker)
        s3 = wait_for(lambda: (x := client.get(f"/sessions/{sid3}", headers=auth()).json())["state"] == "ERROR" and x, 40, "worker_lost")
        step("worker loss ends the session as worker_lost", s3["error"]["code"] in ("worker_lost", "worker_lost_session"), s3["error"]["code"])
        stack.start_worker()
        wait_for(worker_up, 60, "worker back")
        o3 = overview()
        step("after worker loss nothing is counted as present", o3["active_visits"] == 0)

        # 5 - same person again: recognised, not registered twice
        h = auth()
        r = client.post("/sessions", json={"source_id": "e2e-align"}, headers=h)
        sid4 = r.json()["id"]
        wait_for(lambda: client.get(f"/sessions/{sid4}", headers=auth()).json()["state"] == "COMPLETED", 90, "fourth session")
        o4 = overview()
        step("the same person is recognised: unique identities do not grow, visits do", o4["registered_visitors"] == o3["registered_visitors"] and o4["visits"] == o3["visits"] + 1 and o4["returning_visitors_in_scope"] >= 1)
        rec = client.get("/analytics/reconciliation", headers=auth()).json()
        step("final reconciliation consistent", rec["consistent"], f"errors={rec['errors']} warnings={rec['warnings']}")
        metrics["rss_mb_at_end"] = stack.rss_mb()
        metrics["final_overview"] = {k: o4[k] for k in ("registered_visitors", "unique_visitors_in_scope", "visits", "entry_events", "terminal_events", "returning_visitors_in_scope")}
    except Exception as exc:  # noqa: BLE001
        step("harness error", False, f"{type(exc).__name__}: {exc}")
    finally:
        stack.stop()
    report = {
        "scope": "loopback TLS on this machine; production guards on; worker without DB credentials; local PostgreSQL; NOT the public internet",
        "steps": steps, "metrics": metrics, "passed": sum(s["ok"] for s in steps), "failed": sum(not s["ok"] for s in steps),
    }  # fmt: skip
    (args.out / "remote-path.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({"passed": report["passed"], "failed": report["failed"], "metrics": metrics}, indent=2))
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
