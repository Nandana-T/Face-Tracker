# ADR 0012 – Multi-face tracker: own ByteTrack-style implementation

Status: accepted (Phase 2). Scope: temporary, session-scoped tracks only – never a visitor identity.

## Context
Detections arrive from YOLO11n-face on every (N+1)th frame (`frame_skip=2` ⇒ every 3rd frame, ≈ 8 analysed frames/s on this CPU).
Faces are small (median ≈ 30–60 px in the hall footage, down to 21 px), often 0–3 per frame, sometimes 20 (4K crowd), and move
noticeably between analysed frames. The tracker must run on CPU next to the detector, must not add a heavy dependency, and must
behave the same whatever the FPS or `frame_skip`.

## Options considered
| Option | CPU cost | Handles weak detections | Frame-skip / irregular timing | Dependencies / licence | Verdict |
|---|---|---|---|---|---|
| **ByteTrack** (Zhang et al., 2022; MIT code) | very low (IoU + Kalman) | **yes** – second association pass with low-score detections | needs explicit `dt` (we add it) | reference code pulls `lap`/`cython_bbox`/torch-era utilities | **algorithm chosen**, own implementation |
| SORT (Bewley et al., 2016; GPL-3.0 reference code) | lowest | no – low-score detections are discarded or create noise | fixed-step Kalman | GPL-3.0 reference implementation is incompatible with an unrestricted project; scipy `linear_sum_assignment` | rejected (no weak-detection handling, licence of the reference code) |
| OC-SORT (Cao et al., 2023; MIT) | low | partly | helps non-linear motion/occlusion recovery | same stack as above | not needed yet; its observation-centric re-update is a candidate if long occlusions matter later |
| DeepSORT / StrongSORT / BoT-SORT | high (appearance network) | yes | – | extra CNN, torch | **rejected**: an appearance model is face *recognition* by another name and is out of scope for Phase 2 |
| Third-party `bytetracker` / `supervision` packages | low | yes | no `dt` API | extra dependency, numpy/scipy pins | rejected: we need time-based lifecycle and would wrap them anyway |

## Decision
Implement the **ByteTrack association idea** ourselves in `app/vision/tracking/` (≈ 350 lines, `numpy` only – already a
dependency; no scipy, no torch, no new licence):

* IoU association of Kalman-predicted track boxes with detections, greedy best-IoU-first with a stable tie-break
  (deterministic and unit-testable); at ≤ 200 tracks the difference from optimal assignment is negligible, and the Hungarian
  algorithm is only used in the *evaluation* code.
* Two stages à la ByteTrack: confirmed/lost tracks × high-score detections, then leftovers × low-score detections (low-score
  detections may extend a track but never create one), then tentative tracks × remaining high detections.
* Four independent constant-velocity Kalman filters (centre x/y, width, height) with explicit `dt` seconds and size-relative noise
  – no fixed frame interval is assumed.
* **Time-based** lifecycle (seconds, not frames), see `interfaces.py`.
* Additions beyond textbook ByteTrack, each motivated by a measured problem: *buffered IoU* (boxes grown by 10 % per side) because
  12–30 px faces jitter more than their own size; a lower gate and velocity damping for LOST tracks; unobserved tentative tracks
  are not published.

## Frame skipping: update only on analysed frames
Chosen over "predict on skipped frames". Skipped frames are never shown, so predicting there would only create guessed boxes styled
like results and extra work. The tracker receives the real timestamp of each analysed frame, so `dt` is 120 ms at `frame_skip=2`
on 25 fps footage, 40 ms at `frame_skip=0` and 240 ms at `frame_skip=5`, and age/expiry/velocity are correct in seconds. Cost of
the choice: motion between analysed frames is larger, which hurts IoU matching at large skips (measured in the Phase 2 report).

## Consequences / limits
* Motion + overlap only. Two faces that cross while overlapping, or one hidden for longer than `max_missing_seconds` (2 s), can
  swap or lose their id; the person then gets a **new** track id. This is documented in the UI, the API and the report, and is not
  papered over. Recognition-based re-linking is Phase 3's job; visit logic (Phase 4) must tolerate id changes.
* No appearance cues means the tracker cannot tell apart people who walk together and overlap.
* Defaults were calibrated on proxy metrics and a synthetic ground-truth benchmark because the footage has **no labels**; true
  accuracy on real footage is unmeasured. See `docs/phase-2-report.md`.
* Licensing: the tracker is original code in this repository; ByteTrack (MIT) is cited as the source of the idea. No new
  dependency was added.
