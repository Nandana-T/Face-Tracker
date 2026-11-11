r"""Stop a stack started by dev_stack.py.

    python scripts/stop_stack.py <pid>     kill the given PID and every descendant
    python scripts/stop_stack.py --sweep   kill any API / worker process of THIS repository's backend

Uses psutil's real parent links (Windows does not kill children when a parent dies, and the venv
python.exe launcher adds an extra level). Safe to call for a PID that no longer exists.

``--sweep`` exists because a supervisor that is killed abruptly (for example one started from inside a
Playwright test worker) can die before it stops its children, leaving an orphaned API and worker whose
parent link no longer points anywhere. It only touches processes whose working directory is this
repository's ``backend`` folder and whose command line is the API or the worker entry point.
"""

import sys
from pathlib import Path

import psutil

BACKEND = (Path(__file__).resolve().parents[1] / "backend").resolve()
ENTRY_POINTS = ("app.main:app_factory", "app.workers.video_worker")


def kill_family(root: psutil.Process) -> int:
    family = [*root.children(recursive=True), root]
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
    return len(family)


def sweep() -> int:
    victims: list[psutil.Process] = []
    for p in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cmd = " ".join(p.info["cmdline"] or [])
            cwd = p.info["cwd"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if cwd and Path(cwd).resolve() == BACKEND and any(e in cmd for e in ENTRY_POINTS):
            victims.append(p)
    total = 0
    for p in victims:
        try:
            total += kill_family(p)
        except psutil.NoSuchProcess:
            pass
    print(f"swept {total} process(es)")
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--sweep":
        return sweep()
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: stop_stack.py <pid> | --sweep", file=sys.stderr)
        return 2
    try:
        root = psutil.Process(int(sys.argv[1]))
    except psutil.NoSuchProcess:
        print("already stopped")
        return 0
    print(f"stopped {kill_family(root)} process(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
