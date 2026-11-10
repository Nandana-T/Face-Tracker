r"""Run the API and the video worker together (development and E2E tests).

    python scripts/dev_stack.py [--exit-with-parent]

Both are separate OS processes (the worker never shares the API's event loop); this script only
supervises them. If either exits, the other is stopped and the exit code is propagated.
Environment variables (DATABASE_URL, CONFIG_FILE, ...) are passed through unchanged.

``--exit-with-parent``: also shut everything down when any of the nearest launching processes
disappears. On Windows a venv's python.exe is a small launcher that starts the real interpreter and
test runners start commands through a shell; if the runner kills only part of that chain, this flag
prevents orphaned API/worker processes.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

BACKEND = Path(__file__).resolve().parents[1] / "backend"
PORT = os.environ.get("API_PORT", "8000")


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        parent = psutil.Process(proc.pid)
        family = [*parent.children(recursive=True), parent]
    except psutil.NoSuchProcess:
        return
    for p in family:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(family, timeout=8)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass


def main() -> int:
    watch_parent = "--exit-with-parent" in sys.argv
    # Launcher chain, e.g. venv python.exe -> cmd.exe (shell) -> test runner. A runner that
    # terminates only the shell must not leave the API and worker behind, so every one of the
    # nearest ancestors is watched.
    ancestors = [p.pid for p in psutil.Process().parents()[:3]] if watch_parent else []
    api = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app_factory", "--factory",
         "--host", "127.0.0.1", "--port", PORT],
        cwd=BACKEND,
    )
    worker = subprocess.Popen([sys.executable, "-m", "app.workers.video_worker"], cwd=BACKEND)
    try:
        while api.poll() is None and worker.poll() is None:
            if any(not psutil.pid_exists(pid) for pid in ancestors):
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        kill_tree(worker)
        kill_tree(api)
    return api.returncode or worker.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
