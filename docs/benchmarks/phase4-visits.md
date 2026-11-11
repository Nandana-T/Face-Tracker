# Phase 4 benchmark – visit lifecycle on the real hackathon footage

Harness: `backend/benchmarks/evaluate_visits_video.py` (`run`, `ab`). It mirrors the API path in-process with the **production classes**: pipeline frames (track metadata sampled at the stream cadence, 15 fps, in video time) → `VisitManager`; recognition observations → `IdentityManager` → identity bound to tracks → `VisitManager`; every entry/terminal action → `EventRecorder` on a **real PostgreSQL** (embedded local server, throw-away schema), real evidence files and a real `events.log`.
Machine: i5-1240P, CPU only, ONNX Runtime; quality gate, thresholds and models exactly as in Phase 3 (the gate was **not** relaxed to obtain more events). Clips: the 6 hackathon clips used in Phase 3 (5 × 501 analysed frames, 1 × 80 at 4K), raw results in `datasets/evidence/visits/` (git-ignored: it shows real people).

**The footage has no identity labels. Nothing below is a people count or an accuracy figure.**

## Coverage and event integrity (all 6 clips, one registry, in sequence)
| Clip | Confirmed tracks | Tracks bound to a visitor | Visits | Ended by |
|---|---|---|---|---|
| record_…183903 | 12 | 3 | 3 | absence ×3 |
| record_…184807 | 15 | 2 | 2 | absence ×2 |
| record_…185210 | 5 | 0 | 0 | – (no face passed the gate) |
| record_…184403 | 7 | 1 | 1 | observation ended (clip ended) |
| record_…190324 | 5 | 1 | 1 | absence |
| video_sample1 (4K crowd) | 30 | 2 | 2 | observation ended ×2 |
| **Total** | **74** | **9** | **9** | absence 6 · observation ended 3 · interrupted 0 |

* **Event coverage is limited by recognition, by design:** only 9 of 74 confirmed tracks (12 %) ever belonged to a visit; the rest never passed the unchanged quality gate or never became consistent enough to be recognised/registered (blur, profile, small, looking down, masks). Faces that are never recognised produce **no** event - there is no "unknown person" visit.
* **Integrity:** 9 visits, 9 entry + 9 exit events, **every visit has exactly two events**, 0 exits before entries, 18/18 evidence images stored (0 missing, 0 unavailable), `events.log` vs database: 18 database events / 18 logged / **0 missing, 0 duplicated, 0 unknown**, 0 pending, 0 dead; two consecutive reconciliation passes found nothing to repair; 0 recorder errors, 0 retries.
* **Manual review (one reviewer – me – by eye, not independent):** entry and exit crops of all 9 visits show the same person in each pair; times are ordered and spans are 4.4–8.6 s. Two crops show the quality limits that remain: V00008 wears a mask (masks are not detected - Phase 3 limitation) and V00009 is blurry. This checks internal consistency, **not** whether the person physically left: that needs ground truth that does not exist here.
* Visits ≠ visitors ≠ people: 9 visits belong to 9 distinct registry entries; whether any of those entries are the same real person is unknown (Phase 3: cross-clip recognition unverified). No unique-visitor count is claimed.

## Sensitivity to the grace period (same footage, only `visits.exit_after_seconds` changed)
| `exit_after_seconds` | Visits | Ended by absence / observation ended | Visits per visitor | Spans |
|---|---|---|---|---|
| 3 s | 9 | 6 / 3 | 1.0 | 4.4–8.6 s |
| **8 s (default)** | 9 | 6 / 3 | 1.0 | 4.4–8.6 s |
| 15 s | 10 | 5 / 5 | 1.0 | 4.4–8.6 s |
3 s and 8 s are identical. The extra visit at 15 s is an extra *registered visitor* in that run (recognition is timing-dependent: its queue drops work when busy, so identities differ slightly between runs), not a split visit. No visitor left and returned inside a clip, so **real footage never exercised the reacquisition rule** (it is covered by unit and relay tests) and cannot calibrate the defaults; they remain engineering choices.

## Latency (real PostgreSQL, real files, real `events.log`)
| Step | Mean | Max | n |
|---|---|---|---|
| Visit state machine, per processed frame (`on_frame`) | 17–26 µs (p95 ≤ 40 µs) | 349 µs | ≈ 1.2 k frames |
| Evidence image write (validate + atomic write + fsync) | 3.0 ms | 4.4 ms | 18 |
| Database transaction (visit + event + outbox) | 5.8 ms | 22.6 ms | 18 |
| `events.log` line (claim + write + acknowledgement) | 6.0 ms | 10.5 ms | 18 |
| Whole event, end to end | 14–18 ms | 37.4 ms | 18 |
All of it runs **off the frame path** (bounded queue + one consumer task); the frame path pays only the state machine (microseconds). Outbox retry costs one more claim + write (≈ the log step); it is exercised by tests, not separately benchmarked.

## Throughput, CPU and memory (visit layer on vs off; recognition on in both; 600 analysed frames, median of 3 runs)
| Clip | Wall | CPU time | Peak RSS | Detector ms/frame |
|---|---|---|---|---|
| Hall clip, without / with | 15.4 s / 15.9 s (+3 %) | 60.1 s / 60.8 s (+1 %) | 563 / 563 MB | 63.5 / 65.3 |
| 4K crowd, without / with | 8.2 s / 8.3 s (+1 %) | 49.0 s / 49.2 s (+0.4 %) | 920 / 925 MB | 80.1 / 80.6 |
The differences are inside the run-to-run spread of the "without" runs (15.4–16.3 s), so the visit layer's cost is **not distinguishable from noise** here. Memory: the layer keeps at most two ≤ 192 px JPEG crops per track (cap 512 tracks ≈ ≤ 15 MB worst case; typically a few hundred KB); the measured RSS difference is ≤ 5 MB.

## Not measured / not covered
Real RTSP disconnect and reconnect (the rules are unit-tested; no camera was available), GPU, Docker/Render/Vercel, a multi-API deployment, hosted-database latency (the hosted DB is still at migration 0002; local PostgreSQL numbers are lower than a remote round trip), long-running soak tests, and correctness of exit times against physical reality.
