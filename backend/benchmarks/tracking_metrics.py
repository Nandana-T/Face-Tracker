"""Tracking metrics (CLEAR-MOT: MOTA, ID switches, fragmentation; IDF1) and a synthetic benchmark.

Evaluation method (documented in docs/phase-2-report.md):

* Ground truth (GT) = scripted trajectories with known identities, sampled at the analysed frames.
* Hypotheses = the tracker's CONFIRMED tracks that were *observed* in that frame (predicted boxes of
  LOST tracks and TENTATIVE tracks are not scored, which is the conservative choice).
* Per frame, GT and hypotheses are matched at IoU >= 0.5 (CLEAR-MOT rule: a previous match is kept
  while it still overlaps >= 0.5, remaining pairs are assigned greedily by IoU).
* MOTA = 1 - (FN + FP + IDSW) / GT;  IDSW = a GT object matched to a different hypothesis id than its
  previous match;  fragmentation = a GT object's matched status goes matched -> unmatched -> matched.
* IDF1 = 2 IDTP / (2 IDTP + IDFP + IDFN) with the optimal one-to-one GT<->track id assignment
  maximising the number of matched frames (Hungarian algorithm below).
* HOTA is not computed.

This is a SYNTHETIC benchmark: it validates the algorithm and the metric code against known truth. It
says nothing about accuracy on real footage, whose detections are far less regular.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.vision.detection.interfaces import FaceDetection
from app.vision.tracking.interfaces import TrackState
from app.vision.tracking.tracker import FaceByteTracker, TrackerConfig, iou_matrix

Box = tuple[float, float, float, float]
FRAME = (1920, 1080)


# ------------------------------------------------------------------------------ Hungarian
def hungarian_max(weights: list[list[int]]) -> list[tuple[int, int]]:
    """Optimal assignment maximising total weight (rectangular matrices allowed)."""
    if not weights or not weights[0]:
        return []
    n_rows, n_cols = len(weights), len(weights[0])
    size = max(n_rows, n_cols)
    big = max(max(r) for r in weights)
    cost = [
        [big - (weights[i][j] if i < n_rows and j < n_cols else 0) for j in range(size)]
        for i in range(size)
    ]
    u = [0] * (size + 1)
    v = [0] * (size + 1)
    p = [0] * (size + 1)
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [math.inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, size + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return [(p[j] - 1, j - 1) for j in range(1, size + 1) if p[j] - 1 < n_rows and j - 1 < n_cols]


# ------------------------------------------------------------------------------ metrics
@dataclass
class MotResult:
    gt: int = 0
    fn: int = 0
    fp: int = 0
    idsw: int = 0
    fragmentations: int = 0
    idtp: int = 0
    idfp: int = 0
    idfn: int = 0
    gt_tracks: int = 0
    hyp_tracks: int = 0

    @property
    def mota(self) -> float:
        return 1 - (self.fn + self.fp + self.idsw) / self.gt if self.gt else float("nan")

    @property
    def idf1(self) -> float:
        d = 2 * self.idtp + self.idfp + self.idfn
        return 2 * self.idtp / d if d else float("nan")

    def as_dict(self) -> dict[str, Any]:
        return {
            "gt_boxes": self.gt,
            "false_negatives": self.fn,
            "false_positives": self.fp,
            "id_switches": self.idsw,
            "fragmentations": self.fragmentations,
            "MOTA": round(self.mota, 4),
            "IDF1": round(self.idf1, 4),
            "gt_tracks": self.gt_tracks,
            "hypothesis_tracks": self.hyp_tracks,
        }


def evaluate(
    gt_frames: list[dict[int, Box]], hyp_frames: list[dict[int, Box]], iou_threshold: float = 0.5
) -> MotResult:
    res = MotResult()
    last_match: dict[int, int] = {}  # gt id -> hyp id (current CLEAR-MOT association)
    last_hyp_of: dict[int, int] = {}  # gt id -> last hyp id ever matched (for IDSW)
    was_matched: dict[int, bool] = {}
    overlap: dict[tuple[int, int], int] = {}
    gt_len: dict[int, int] = {}
    hyp_len: dict[int, int] = {}
    for gt, hyp in zip(gt_frames, hyp_frames, strict=True):
        res.gt += len(gt)
        for gid in gt:
            gt_len[gid] = gt_len.get(gid, 0) + 1
        for hid in hyp:
            hyp_len[hid] = hyp_len.get(hid, 0) + 1
        g_ids, h_ids = list(gt), list(hyp)
        m = iou_matrix([gt[g] for g in g_ids], [hyp[h] for h in h_ids])
        matches: dict[int, int] = {}
        for gi, g in enumerate(g_ids):
            prev = last_match.get(g)
            if prev in hyp:
                hj = h_ids.index(prev)
                if m[gi, hj] >= iou_threshold:
                    matches[g] = prev
        taken_h = set(matches.values())
        pairs = sorted(
            (
                (-float(m[gi, hj]), gi, hj)
                for gi, g in enumerate(g_ids)
                for hj, h in enumerate(h_ids)
                if g not in matches and h not in taken_h and m[gi, hj] >= iou_threshold
            )
        )
        used_g: set[int] = set()
        used_h: set[int] = set()
        for _, gi, hj in pairs:
            if gi in used_g or hj in used_h:
                continue
            used_g.add(gi)
            used_h.add(hj)
            matches[g_ids[gi]] = h_ids[hj]
        for g, h in matches.items():
            overlap[(g, h)] = overlap.get((g, h), 0) + 1
            if g in last_hyp_of and last_hyp_of[g] != h:
                res.idsw += 1
            last_hyp_of[g] = h
            if was_matched.get(g) is False:
                res.fragmentations += 1
        for g in g_ids:
            was_matched[g] = g in matches
        last_match = dict(matches)
        res.fn += len(g_ids) - len(matches)
        res.fp += len(h_ids) - len(matches)

    res.gt_tracks, res.hyp_tracks = len(gt_len), len(hyp_len)
    gids, hids = sorted(gt_len), sorted(hyp_len)
    weights = [[overlap.get((g, h), 0) for h in hids] for g in gids]
    idtp = sum(weights[i][j] for i, j in hungarian_max(weights)) if gids and hids else 0
    res.idtp = idtp
    res.idfn = sum(gt_len.values()) - idtp
    res.idfp = sum(hyp_len.values()) - idtp
    return res


# ------------------------------------------------------------------------------ scenarios
def box(cx: float, cy: float, w: float = 90, h: float = 110) -> Box:
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


@dataclass
class Scenario:
    name: str
    duration_s: float
    trajectories: dict[int, Callable[[float], Box | None]]  # None = not in the picture
    # Per-object intervals [(start_s, end_s)] in which the detector fails (occlusion).
    occlusions: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    note: str = ""


def line(
    x0: float,
    y0: float,
    vx: float,
    vy: float,
    t0: float = 0,
    t1: float = 1e9,
    w: float = 90,
    h: float = 110,
) -> Callable[[float], Box | None]:
    def f(t: float) -> Box | None:
        if not (t0 <= t <= t1):
            return None
        return box(x0 + vx * (t - t0), y0 + vy * (t - t0), w, h)

    return f


def scenarios() -> list[Scenario]:
    rnd = random.Random(7)
    crowd = {
        i: line(
            rnd.uniform(150, 1750),
            rnd.uniform(150, 900),
            rnd.uniform(-60, 60),
            rnd.uniform(-40, 40),
            w=rnd.uniform(60, 110),
            h=rnd.uniform(75, 135),
        )
        for i in range(1, 16)
    }
    return [
        Scenario("single_straight", 12, {1: line(200, 500, 90, 0)}),
        Scenario(
            "three_parallel",
            12,
            {1: line(200, 200, 80, 0), 2: line(200, 500, 70, 0), 3: line(200, 800, 60, 0)},
        ),
        Scenario(
            "crossing_clear",
            12,
            {1: line(300, 400, 120, 10), 2: line(1500, 560, -120, -10)},
            note="two faces cross at different heights (little overlap)",
        ),
        Scenario(
            "crossing_overlapping",
            12,
            {1: line(300, 500, 130, 0), 2: line(1500, 500, -130, 0)},
            note="two faces cross on the same line: overlapping boxes, identity is ambiguous",
        ),
        Scenario(
            "short_occlusion_0.8s",
            12,
            {1: line(300, 500, 100, 0)},
            {1: [(5.0, 5.8)]},
            note="detector fails for 0.8 s (< max_missing_seconds)",
        ),
        Scenario(
            "long_occlusion_4s",
            14,
            {1: line(300, 500, 100, 0)},
            {1: [(5.0, 9.0)]},
            note="4 s gap (> max_missing_seconds): a new id is EXPECTED",
        ),
        Scenario(
            "enter_leave",
            14,
            {1: line(100, 400, 120, 0, 0, 8), 2: line(1800, 700, -120, 0, 4, 12)},
        ),
        Scenario("crowd_15", 10, crowd, note="15 slowly moving faces"),
    ]


@dataclass(frozen=True)
class Noise:
    name: str
    miss_rate: float = 0.0
    jitter: float = 0.0  # std, as a fraction of box size
    false_positives: float = 0.0  # expected spurious boxes per analysed frame
    low_conf_rate: float = 0.0  # fraction of true detections reported with confidence 0.5-0.6


NOISES = [
    Noise("clean"),
    Noise("realistic", miss_rate=0.15, jitter=0.04, false_positives=0.1, low_conf_rate=0.2),
    Noise("harsh", miss_rate=0.35, jitter=0.08, false_positives=0.4, low_conf_rate=0.35),
]


def run_scenario(
    sc: Scenario, step_ms: int, noise: Noise, config: TrackerConfig | None = None, seed: int = 11
) -> MotResult:
    rnd = random.Random(seed)
    tracker = FaceByteTracker(config or TrackerConfig())
    gt_frames: list[dict[int, Box]] = []
    hyp_frames: list[dict[int, Box]] = []
    steps = int(sc.duration_s * 1000 / step_ms)
    for k in range(steps):
        t = k * step_ms / 1000
        gt: dict[int, Box] = {}
        dets: list[FaceDetection] = []
        for gid, traj in sc.trajectories.items():
            b = traj(t)
            if b is None:
                continue
            gt[gid] = b
            if any(a <= t <= z for a, z in sc.occlusions.get(gid, [])):
                continue
            if rnd.random() < noise.miss_rate:
                continue
            w, h = b[2] - b[0], b[3] - b[1]
            jit = [rnd.gauss(0, noise.jitter) * s for s in (w, h, w, h)]
            conf = (
                rnd.uniform(0.5, 0.6)
                if rnd.random() < noise.low_conf_rate
                else rnd.uniform(0.75, 0.97)
            )
            dets.append(
                FaceDetection(b[0] + jit[0], b[1] + jit[1], b[2] + jit[2], b[3] + jit[3], conf)
            )
        fp = 0
        while rnd.random() < noise.false_positives / (1 + fp) and fp < 3:
            fp += 1
            x, y = rnd.uniform(50, FRAME[0] - 150), rnd.uniform(50, FRAME[1] - 150)
            dets.append(FaceDetection(x, y, x + 70, y + 85, rnd.uniform(0.5, 0.7)))
        dets = [d for d in dets if d.x2 > d.x1 and d.y2 > d.y1]
        out = tracker.update(dets, int(k * step_ms), k, FRAME)
        gt_frames.append({gid: _clip(b) for gid, b in gt.items()})
        hyp_frames.append(
            {
                s.track_id: s.bbox
                for s in out.tracks
                if s.state is TrackState.CONFIRMED and s.observed
            }
        )
    return evaluate(gt_frames, hyp_frames)


def _clip(b: Box) -> Box:
    return (max(0.0, b[0]), max(0.0, b[1]), min(float(FRAME[0]), b[2]), min(float(FRAME[1]), b[3]))
