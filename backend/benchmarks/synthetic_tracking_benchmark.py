r"""Synthetic ground-truth tracking benchmark (see tracking_metrics.py for the method).

.venv\Scripts\python.exe backend\benchmarks\synthetic_tracking_benchmark.py --out docs\benchmarks
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tracking_metrics import NOISES, run_scenario, scenarios  # noqa: E402

# 40 ms = every frame at 25 fps (frame_skip 0); 120 ms = frame_skip 2; 240 ms = frame_skip 5
STEPS = (40, 120, 240)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    rows = []
    print(
        f"{'scenario':24} {'noise':10} {'step':>5} {'MOTA':>7} {'IDF1':>7} {'IDSW':>5} {'FRAG':>5} {'FN':>5} {'FP':>4} gt/hyp"
    )
    for sc in scenarios():
        for noise in NOISES:
            for step in STEPS:
                r = run_scenario(sc, step, noise)
                rows.append(
                    {"scenario": sc.name, "noise": noise.name, "step_ms": step, **r.as_dict()}
                )
                print(
                    f"{sc.name:24} {noise.name:10} {step:>5} {r.mota:7.3f} {r.idf1:7.3f} "
                    f"{r.idsw:5d} {r.fragmentations:5d} {r.fn:5d} {r.fp:4d} {r.gt_tracks}/{r.hyp_tracks}"
                )
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "phase2-tracking-synthetic.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
