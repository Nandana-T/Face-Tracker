# Final evaluation on the hackathon footage – what was measured, and what was not

Footage: 23 clips under `datasets/hackathon` (22 hall clips 2688×1520 ≈ 60 s, one 4K crowd sample), already downloaded; **no copies were made**. It has **no person-level or track-level labels**. Evidence images show real people and are git-ignored – nothing below publishes a face. The metric families are kept apart on purpose; none of them is an accuracy claim about people.

## Reproduce (all local, all on D:)
```powershell
.venv\Scripts\python.exe backend\benchmarks\evaluate_video.py --videos datasets\hackathon --out datasets\evidence\hackathon        # detection (+ render_video_report.py)
.venv\Scripts\python.exe backend\benchmarks\evaluate_tracking.py ...                                                        # tracking diagnostics
.venv\Scripts\python.exe backend\benchmarks\evaluate_recognition_video.py run --out datasets\evidence\recognition\video     # recognition + registration
.venv\Scripts\python.exe backend\benchmarks\evaluate_visits_video.py run --out datasets\evidence\visits                     # visits, events, evidence, log verification
.venv\Scripts\python.exe scripts\reconcile_counts.py                                                                         # count reconciliation
```
Safe sample of the output formats (fixture, not footage): `docs/sample-output/`.

## 1. Detection diagnostics ([real-video-validation.md](real-video-validation.md))
33,284 frames read, 11,102 analysed (`frame_skip=2`), 4,942 face detections, 2,860 analysed frames (26 %) with ≥ 1 face, 0 corrupt frames; ONNX inference mean 55 ms/frame (78 ms for the 4K clip), CPU ≈ 24 %, peak RSS 520–881 MB; 4K runs about real time with no headroom. **These are detections, not people.** Precision/recall against labelled boxes was not measured on this footage.

## 2. Tracking diagnostics ([phase-2-report.md](phase-2-report.md), [benchmarks/phase2-tracking.md](benchmarks/phase2-tracking.md))
On the six hall clips at the default `frame_skip=2`: 65 tracks created, 47 confirmed; confirmed tracks last 2.5–11.9 s at the median. "Suspected fragmentation" is a proxy, not an error rate. ID-switch/fragmentation accuracy is only measured on a synthetic ground-truth benchmark.

## 3. Recognition evaluation
* **Labelled** (LFW verification, 2,996 impostor pairs): 99.4 % pair accuracy; thresholds chosen from it ([phase-3-report.md](phase-3-report.md)).
* **Labelled identity-level** (LFW identities through the real pipeline, 14 people): 14 registered vs 14 true, but 12 correct, 1 missed, 1 duplicate, 0 wrong merges ([phase-5-report.md §8](phase-5-report.md)) – synthetic video, optimistic.
* **Real footage: no recognition accuracy exists** (no labels). Operational diagnostics only: 74 confirmed tracks over 6 clips, 9 resolved to a registered identity (≈ 12 %); the quality gate rejects most real faces (blur, profile, small, looking down); a masked face was registered (masks are not detected). The entry/exit crops of all 9 visits show the same person by eye (one reviewer).

## 4. Registered identity counts (not people)
6 clips → 9 registered identities (Phase 3), identical in the Phase 4 run; whether any two are the same real person, or whether people were missed, is **unknown**. Cross-clip recognition was not observed. Registered identities are counted exactly; physical people are not.

## 5. Visit / event correctness ([phase-4-report.md](phase-4-report.md))
9 visits, 9 entry + 9 terminal events (6 by absence, 3 observation-ended, 0 interrupted), every visit exactly two events, 0 exits before entries, 18/18 evidence images, `events.log` ≡ database, reconciliation found nothing to repair. Event *coverage* is limited by recognition (9 of 74 tracks); exit-time correctness against reality is unverifiable.

## 6. End-to-end performance
Event persistence ≈ 14–18 ms/event (off the frame path); visit layer cost within noise; analytics queries 5–660 ms from 1 k to 100 k synthetic visits ([benchmarks/phase5-analytics.md](benchmarks/phase5-analytics.md)); production-like remote path (loopback TLS): HTTPS round trip ≈ 3.4 ms, worker connect 5.4 s, reconnect after API restart 4–6.5 s, start → running 0.7 s, API RSS 115 MB, worker RSS 171–181 MB; local RTSP server: connect → running 1.7–2.5 s, interruption detected in 0.4 s, processing resumed 3.7 s after the source returned ([phase-6-report.md](phase-6-report.md)). Hosted-network latency was not measured (nothing is deployed).

## What this evaluation cannot show
True person counts on the footage; recognition recall/precision on real faces; behaviour with a physical camera; GPU performance; hosted deployment behaviour.
