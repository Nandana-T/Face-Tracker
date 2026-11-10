# Phase 2 tracking benchmark

Machine: the same Windows 11 / Intel i5-1240P laptop as the Phase 1 benchmark (CPU only, no GPU), ONNX Runtime with
`intra_op_threads=4`, `cpu_spin_wait=false`, shipped `config/config.json` tracking defaults. Measured 2026-11-10.
Reproduce: `backend\benchmarks\evaluate_tracking.py detect|track|ab|frames` (real footage) and
`backend\benchmarks\synthetic_tracking_benchmark.py` (synthetic ground truth). Raw numbers: `phase2-tracking-synthetic.json`
(this folder) and, for the real footage (git-ignored, real people), `datasets/evidence/tracking/`.

**What is measured, what is a proxy, what is not available**

| Kind | Items |
|---|---|
| **Measured** (direct timings/counters) | tracker time per update, CPU time, wall time, RSS, track counts, track durations, recoveries |
| **Synthetic ground truth** (known trajectories; validates algorithm + metric code only) | MOTA, IDF1, ID switches, fragmentation, false positives/negatives – *never* computed on the real footage |
| **Proxy / estimate** (no labels exist for the hackathon footage) | "suspected fragmentation" (a new confirmed track that starts within 3 s and 1.5 face-widths of where another confirmed track was last detected) – a hint, not a count of errors |
| **Manual review** (one AI reviewer, ≈ 10 sheets/frames) | qualitative observations in [`../phase-2-report.md`](../phase-2-report.md) |
| **Not available** | real IDF1/MOTA/HOTA, true ID-switch rate, false-track rate and miss rate on real footage; real RTSP; GPU |

## 1. Tracking cost on real footage (direct measurements)

Tracker only (replay of the cached detections, `frame_skip=2`):

| Footage | Analysed frames | Mean ms / update | p99 ms | Max simultaneous tracks |
|---|---|---|---|---|
| 6 hall clips (2688×1520, 60 s each) | 501 each | 0.011 – 0.056 | 0.09 – 0.23 | 2 – 5 |
| `video_sample1.mp4` crowded 4K (8 s) | 80 | **0.42** | **0.93** | 20 |

For scale: the detector needs ≈ 50 ms (hall) to ≈ 69 ms (4K) per analysed frame, so the tracker is **≈ 0.1–0.3 % (hall) and ≈ 1 % (4K crowd)
of the inference time**. Memory: tracks are bounded by `max_tracks`; removed tracks are freed immediately.

A/B on the real pipeline (real YOLO; first 600 source frames; `frame_skip=2`; median of 3 interleaved runs; whole-process figures):

| Clip | | Wall s | CPU s (process) | Peak RSS MB | Detector ms / frame | Tracker ms / frame |
|---|---|---|---|---|---|
| hall 183903 | without tracker | 13.57 | 54.61 | 464 | 52.4 | – |
| | with tracker | 13.89 | 54.66 | 464 | 53.6 | 0.17 |
| 4K sample | without tracker | 7.37 | 38.08 | 820 | 69.1 | – |
| | with tracker | 7.44 | 38.28 | 817 | 69.1 | 0.73 |

**Interpretation:** the end-to-end difference (≤ 2.4 % wall, ≤ 0.5 % CPU, ≤ 3 MB RSS) is **smaller than the run-to-run spread**
(identical runs without tracker spread 49.4–54.8 CPU-s on the hall clip), so it cannot be resolved by this A/B; the directly timed tracker cost above is the
reliable figure. No extra threads are created (the tracker runs in the existing inference thread), so there is no thread
oversubscription, and nothing here approaches the real-time budget. Historical reference (Phase 1.5): 14.3 analysed frames/s,
55 ms mean inference, ≈ 520 MB RSS – unchanged within noise.

## 2. Tracks on real footage (7-clip subset; **no ground truth**)

Subset (chosen from the Phase 1.5 per-video results to cover the requested scene types): `183903` (up to 3 faces), `184807`
(up to 5, the busiest hall clip), `185210` (people entering/leaving), `184403` (partial visibility), `184605` (sparse, small faces –
21–30 px), `190324` (two people walking together/apart – crossing candidates), `video_sample1` (crowded 4K, up to 20 faces, fast motion).
Not in the footage: deliberate occlusion/crossing staging – these situations occur only incidentally, which is why the synthetic benchmark exists.

All clips, summed. "Tracks created" counts every temporary track including rejected tentative ones; **none of these numbers is a count of people**.

| `frame_skip` | dt (25 fps) | Analysed frames | Raw detections | Tracks created | Confirmed | Rejected (tentative) | Confirmed tracks expired | Lost→confirmed recoveries | Confirmed < 1 s | Suspected fragmentations (proxy) |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 40 ms | 9,252 | 9,346 | 106 | 84 | 22 | 59 | 247 | 14 | 3 |
| **2 (default)** | 120 ms | 3,086 | 3,113 | 116 | 77 | 36 | 53 | 72 | 11 | 3 |
| 5 | 240 ms | 1,546 | 1,560 | 155 | 66 | 87 | 46 | 25 | 10 | 2 |

Hall clips only (6 clips, 2688×1520): skip 0 → 68 created / 53 confirmed; skip 2 → 65 / 47; skip 5 → 77 / 42 (35 rejected).

**Impact of frame skipping (measured):** the confirmed-track count falls as skipping rises (84 → 77 → 66) because short appearances
get too few detections to confirm, and rejected tentative tracks rise sharply (22 → 36 → 87). In the crowded 4K clip `frame_skip=5` created **78**
tracks against 38 at `frame_skip=0` – faces move too far between analysed frames for box overlap to hold. `frame_skip=2` is a
reasonable default; values above ≈ 3 are not recommended with tracking in crowds. Whether the tracks at each setting are *correct* is not measurable without labels.

Per-clip medians at `frame_skip=2`: confirmed tracks last 2.5–11.9 s (hall) and 3.4 s (4K) at the median; the sparse small-face clip is the weak
spot (median 0.48 s, 3 of 3 confirmed tracks < 1 s: faces are seen in ≈ 10 % of analysed frames and rarely three times in a row).

### Parameter sensitivity (what the defaults rest on)
Because there are no labels, defaults were chosen from sensitivity sweeps over the 7 clips (tracks created / rejected / suspected
fragmentations, share of raw detections that ended up in confirmed observed tracks) plus the synthetic benchmark:

| Parameter | Tried | Observation |
|---|---|---|
| `high_confidence` | 0.5 / **0.6** / 0.7 | 0.5: coverage 91 % but 176 tracks created (flicker); 0.7: coverage 59 %. 0.6 kept (78 % coverage, 134 created before the later buffer change) |
| `confirm_hits` | 2 / **3** / 4 | fewer hits → more flicker tracks confirmed; 4 → lower coverage (75 %) |
| `tentative_timeout_seconds` | 0.3 / **0.5** / 0.8 | no measurable effect at these frame rates; 0.5 s ≈ 4 analysed frames at the default skip |
| `max_missing_seconds` | 1 / **2** / 3 | almost no effect on the footage (few long gaps); 2 s is a conservative walking-pace limit |
| `match_iou` | 0.1 / **0.2** / 0.3 | 0.3 doubled rejected tracks (small faces rarely overlap that much between frames) |
| `box_buffer` (buffered IoU) | 0 / 0.1 / 0.2 / 0.3 | real footage: rejected tracks 50 → 36 → 24 → 16; **but** the synthetic 15-face scene shows ID switches 2 → 3 → 12 → 26 (10 seeds × 3 time steps, realistic noise). **0.1 chosen**: fewer flicker tracks without a measurable switch cost (all-scenario total 43 at buffer 0 and at 0.1) |
| `lost_velocity_decay`, `lost_match_iou` | 2 / 1 / 0.5 / 0 ; 0.2 / 0.1 / 0.05 | tiny differences on (perfectly linear) synthetic motion; moderate values kept so a face that stops or turns is not extrapolated away |

These are sensitivities, **not an optimisation** against ground truth.

## 3. Synthetic ground-truth benchmark (known trajectories; algorithm validation only)

Method (`tracking_metrics.py`): scripted trajectories sampled at 40 / 120 / 240 ms (= `frame_skip` 0 / 2 / 5 at 25 fps), detections generated with
controlled noise; hypotheses = CONFIRMED tracks observed in the frame; IoU ≥ 0.5 matching; CLEAR-MOT (MOTA, ID switches, fragmentation)
and IDF1 (optimal id assignment by Hungarian algorithm). HOTA not computed. Noise: *clean*; *realistic* (15 % missed detections, 4 % box jitter, 0.1
false positives per frame, 20 % of detections weak 0.5–0.6); *harsh* (35 % / 8 % / 0.4 / 35 %). Seed 11. Values in the table are for the 120 ms step (`frame_skip=2`):

| Scenario | Noise | MOTA | IDF1 | ID switches | Fragm. | GT / hypothesis tracks |
|---|---|---|---|---|---|---|
| single face, straight | clean | 0.980 | 0.990 | 0 | 1 | 1 / 1 |
| single face, straight | realistic | 0.770 | 0.789 | **1** | 14 | 1 / 2 |
| three parallel faces | clean / realistic | 0.980 / 0.830 | 0.990 / 0.907 | 0 / 0 | 3 / 40 | 3 / 3 |
| two faces crossing, separated | clean / realistic | 0.980 / 0.805 | 0.990 / 0.892 | 0 / 0 | 2 / 25 | 2 / 2 |
| two faces crossing on the same line (overlapping boxes) | clean / realistic | 0.975 / 0.805 | 0.987 / 0.892 | 0 / 0 | 2 / 25 | 2 / 2 |
| 0.8 s detector failure | clean / realistic | 0.910 / 0.730 | 0.953 / 0.844 | 0 / 0 | 2 / 16 | 1 / 1 |
| **4 s gap (> `max_missing_seconds`)** | clean | 0.664 | 0.412 | 1 | 2 | 1 / **2** (a new id is *expected*) |
| enter / leave | clean / realistic | 0.970 / 0.799 | 0.985 / 0.888 | 0 / 0 | 2 / 18 | 2 / 2 |
| 15 moving faces | clean / realistic | 0.895 / 0.743 | 0.944 / 0.853 | 0 / 0 | 15 / 152 | 15 / 15 |

Reading the table honestly:
* Low MOTA/high "FN" under *realistic* noise is **detector loss that was injected** (15 % misses plus the 2-update confirmation delay), not tracker error; ID switches
  and IDF1 are the tracker-relevant columns. Fragmentation counts "matched → unmatched → matched" runs, so injected misses inflate it.
* The single ID switch in the `single_straight` realistic/120 ms cell is a genuine tracker failure caused by this seed's draw (11) – a false positive close to the
  face while it was missed. It is why the regression guard uses ten fixed seeds: over seeds 1–10 the three simple scenes (single, three parallel, enter/leave) switched **0** times at 40 and 120 ms.
* Overlapping crossing gave 0 switches here only because the two faces keep a constant velocity through the overlap; with motion changes inside
  the overlap, motion-only tracking cannot guarantee identity (ten-seed total for that scene: 8 switches, buffer 0.1).
* Harsh noise (35 % misses, 8 % jitter, many false positives) degrades tracking, most at 240 ms (`frame_skip=5`): e.g. separated crossing 2 switches and 3 hypothesis tracks for 2 faces, 0.8 s failure 2 switches; at 120 ms the harsh runs show 0–1 switches per scenario. Full rows are in the JSON.
* In `crowd_15` clean at 40 ms, FN is 342 of 3,750 ground-truth boxes. Of that, only ≈ 30 is the confirmation delay; the remainder was **not decomposed**
  (likely boxes partly outside the frame or heavily overlapping, which are scored against clipped ground truth). Treat the crowd row's FN as unexplained.
