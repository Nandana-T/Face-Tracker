r"""Stop a stack started by dev_stack.py: kill the given PID and every descendant.

    python scripts/stop_stack.py <pid>

Uses psutil's real parent links (Windows does not kill children when a parent dies, and the venv
python.exe launcher adds an extra level). Safe to call for a PID that no longer exists.
"""

import sys

import psutil


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print("usage: stop_stack.py <pid>", file=sys.stderr)
        return 2
    try:
        root = psutil.Process(int(sys.argv[1]))
        family = [*root.children(recursive=True), root]
    except psutil.NoSuchProcess:
        print("already stopped")
        return 0
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
    print(f"stopped {len(family)} process(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
