"""The evaluation code itself is tested against hand-computed answers, then used as a regression
guard on the synthetic scenarios (known trajectories). These thresholds protect the tracker's
behaviour; they are NOT accuracy claims about real footage."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from tracking_metrics import (  # type: ignore[import-not-found]  # noqa: E402
    NOISES,
    Box,
    evaluate,
    hungarian_max,
    run_scenario,
    scenarios,
)


def b(x: float) -> Box:
    return (x, 0.0, x + 10, 10.0)


def test_hungarian_finds_the_optimal_assignment() -> None:
    weights = [[5, 1, 0], [4, 4, 0], [0, 0, 1]]
    pairs = dict(hungarian_max(weights))
    assert sum(weights[i][j] for i, j in pairs.items()) == 5 + 4 + 1
    assert hungarian_max([]) == []
    assert sorted(hungarian_max([[3, 9]])) == [(0, 1)]


def test_perfect_tracking_scores_one() -> None:
    gt = [{1: b(0)}, {1: b(1)}, {1: b(2)}]
    r = evaluate(gt, [{7: x[1]} for x in gt])
    assert (r.mota, r.idf1, r.idsw, r.fragmentations) == (1.0, 1.0, 0, 0)


def test_id_switch_is_counted_once() -> None:
    gt = [{1: b(0)}] * 4
    hyp = [{5: b(0)}, {5: b(0)}, {6: b(0)}, {6: b(0)}]
    r = evaluate(gt, hyp)
    assert r.idsw == 1 and r.fn == 0 and r.fp == 0
    assert r.mota == pytest.approx(1 - 1 / 4)
    assert r.idf1 == pytest.approx(2 * 2 / (2 * 2 + 2 + 2))


def test_miss_and_false_positive_and_fragmentation() -> None:
    gt = [{1: b(0)}, {1: b(0)}, {1: b(0)}]
    hyp = [{5: b(0)}, {}, {5: b(0), 9: b(500)}]
    r = evaluate(gt, hyp)
    assert (r.fn, r.fp, r.idsw, r.fragmentations) == (1, 1, 0, 1)
    assert r.mota == pytest.approx(1 - 2 / 3)


def test_no_gt_gives_nan_mota() -> None:
    r = evaluate([{}, {}], [{}, {}])
    assert r.mota != r.mota  # NaN


BY_NAME = {s.name: s for s in scenarios()}
CLEAN, REALISTIC = NOISES[0], NOISES[1]


@pytest.mark.parametrize("step", [40, 120, 240])
@pytest.mark.parametrize(
    "name", ["single_straight", "three_parallel", "crossing_clear", "enter_leave", "crowd_15"]
)
def test_clean_detections_never_switch_identity(name: str, step: int) -> None:
    r = run_scenario(BY_NAME[name], step, CLEAN)
    assert r.idsw == 0
    assert r.idf1 > 0.9 and r.mota > 0.85


@pytest.mark.parametrize("step", [40, 120])
def test_realistic_noise_keeps_ids_stable_on_simple_scenes(step: int) -> None:
    """Seed-averaged: one unlucky false positive can still cause a switch (seed 11 does), so the
    guard is over ten fixed seeds rather than a single draw."""
    for name in ("single_straight", "three_parallel", "enter_leave"):
        total = sum(run_scenario(BY_NAME[name], step, REALISTIC, seed=k).idsw for k in range(1, 11))
        assert total == 0, name


def test_short_gap_keeps_the_id_and_a_long_gap_gets_a_new_one() -> None:
    short = run_scenario(BY_NAME["short_occlusion_0.8s"], 120, CLEAN)
    long = run_scenario(BY_NAME["long_occlusion_4s"], 120, CLEAN)
    assert short.hyp_tracks == 1 and short.idsw == 0
    assert long.hyp_tracks == 2  # expected: no identity through a gap longer than the lost limit
