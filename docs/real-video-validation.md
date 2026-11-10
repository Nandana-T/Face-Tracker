# Real-video validation (hackathon footage)

Source: the folder named in the problem statement (Google Drive), downloaded with `gdown` from the public share link - **no sign-in was needed**. 23 files, 228 MB, stored under `datasets/hackathon` (git-ignored). `video_sample1.mp4` is presumably the "sample video"; the other 22 `record_*.mp4` are hall-camera recordings.

Reproduce: `backend\benchmarks\evaluate_video.py --videos datasets\hackathon --out datasets\evidence\hackathon`, then `render_video_report.py`.
The annotated evidence frames (92) and review sheets (16) are in `datasets/evidence/hackathon/` - **git-ignored on purpose: they show real people.**

> All counts below are **face detections in analysed frames**, not visitors or unique people.

## 1. Video properties
* `record_*` (22 files): 2688×1520 @ 25.033 fps, 1502 frames ≈ 60.0 s each, codec `hevc`, ≈ 8.6 MB each.
* `video_sample1.mp4`: 3840×2160 @ 29.97 fps, 240 frames ≈ 8.0 s, codec `h264`, 38.5 MB.
* Total: 33,284 frames, 22.1 minutes. Unreadable/corrupt frames: **0**.

## 2. Processing (the real pipeline, shipped settings)
`frame_skip=2` (every 3rd frame analysed), 4 ORT threads, spin-wait off, confidence ≥ 0.5, run flat out (no real-time pacing), JPEG encode for streaming included.

| | Result |
|---|---|
| Frames read / analysed / skipped | 33,284 / 11,102 / 22,182 |
| Face detections (sum over analysed frames) | 4,942 |
| Analysed frames containing ≥ 1 face | 2,860 of 11,102 (26 %) |
| Detector throughput | 14.3 analysed frames/s overall; 2.7K clips ≈ 12–16/s, the 4K sample 9.4/s |
| Inference latency (ONNX, 640×640) | mean 55.11 ms (clips 48–67 ms; 4K sample 78.68 ms - slower than the 31-40 ms synthetic benchmark because decoding 2.7K/4K frames competes for the same cores) |
| CPU use (process, % of 16-thread machine) | mean 24.1 % (4K sample 32.9 %) |
| Peak memory (RSS) | ≈ 520 MB for 2.7K clips, 881 MB for the 4K sample (large decoded frames) |
| 4K decode headroom | the 4K sample was consumed at 28.1 source frames/s flat out vs its 29.97 fps: **about real time, no headroom** (2.7K clips: ≈ 47/s vs 25 fps) |
| Wall time | 12.9 min for 22.1 min of video (1.7× real time on one pipeline) |

## 3. Faces per sampled frame, per video
| Video | Frames | Analysed | Detections | Mean faces / analysed frame | Max in a frame | Mean / min confidence | Smallest face (px) | Cross-check: both / YOLO-only / YuNet-only |
|---|---|---|---|---|---|---|---|---|
| record_20250620_183903.mp4 | 1502 | 501 | 463 | 0.924 | 3 | 0.69 / 0.501 | 27 | 451 / 12 / 672 |
| record_20250620_184049.mp4 | 1502 | 501 | 201 | 0.401 | 3 | 0.58 / 0.5 | 27 | 194 / 7 / 842 |
| record_20250620_184302.mp4 | 1502 | 501 | 181 | 0.361 | 2 | 0.612 / 0.502 | 27 | 173 / 8 / 397 |
| record_20250620_184403.mp4 | 1502 | 501 | 286 | 0.571 | 3 | 0.638 / 0.5 | 24 | 281 / 5 / 430 |
| record_20250620_184504.mp4 | 1502 | 501 | 157 | 0.313 | 2 | 0.653 / 0.502 | 29 | 149 / 8 / 647 |
| record_20250620_184605.mp4 | 1502 | 501 | 51 | 0.102 | 2 | 0.573 / 0.502 | 30 | 51 / 0 / 524 |
| record_20250620_184705.mp4 | 1502 | 501 | 209 | 0.417 | 3 | 0.617 / 0.501 | 29 | 206 / 3 / 657 |
| record_20250620_184807.mp4 | 1502 | 501 | 531 | 1.06 | 5 | 0.676 / 0.5 | 25 | 522 / 9 / 687 |
| record_20250620_184907.mp4 | 1502 | 501 | 207 | 0.413 | 2 | 0.613 / 0.5 | 31 | 199 / 8 / 529 |
| record_20250620_185008.mp4 | 1502 | 501 | 170 | 0.339 | 2 | 0.607 / 0.5 | 27 | 165 / 5 / 697 |
| record_20250620_185109.mp4 | 1502 | 501 | 78 | 0.156 | 1 | 0.669 / 0.512 | 26 | 76 / 2 / 247 |
| record_20250620_185210.mp4 | 1502 | 501 | 411 | 0.82 | 3 | 0.724 / 0.506 | 25 | 411 / 0 / 414 |
| record_20250620_190324.mp4 | 1502 | 501 | 217 | 0.433 | 2 | 0.76 / 0.521 | 21 | 208 / 9 / 21 |
| record_20250620_190425.mp4 | 1502 | 501 | 111 | 0.222 | 2 | 0.776 / 0.523 | 37 | 110 / 1 / 13 |
| record_20250620_190525.mp4 | 1502 | 501 | 51 | 0.102 | 1 | 0.776 / 0.602 | 45 | 51 / 0 / 3 |
| record_20250620_190626.mp4 | 1502 | 501 | 48 | 0.096 | 2 | 0.725 / 0.514 | 31 | 47 / 1 / 12 |
| record_20250620_190727.mp4 | 1502 | 501 | 38 | 0.076 | 2 | 0.664 / 0.516 | 35 | 29 / 9 / 8 |
| record_20250620_190828.mp4 | 1502 | 501 | 64 | 0.128 | 2 | 0.734 / 0.505 | 37 | 58 / 6 / 19 |
| record_20250620_190928.mp4 | 1502 | 501 | 46 | 0.092 | 1 | 0.696 / 0.527 | 57 | 41 / 5 / 3 |
| record_20250620_191029.mp4 | 1502 | 501 | 11 | 0.022 | 1 | 0.62 / 0.501 | 40 | 9 / 2 / 42 |
| record_20250620_191129.mp4 | 1502 | 501 | 91 | 0.182 | 2 | 0.729 / 0.514 | 30 | 91 / 0 / 7 |
| record_20250620_191230.mp4 | 1502 | 501 | 166 | 0.331 | 3 | 0.714 / 0.509 | 33 | 162 / 4 / 39 |
| video_sample1.mp4 | 240 | 80 | 1154 | 14.425 | 20 | 0.702 / 0.5 | 39 | 1143 / 11 / 454 |

## 4. Independent cross-check (YuNet, MIT licence) - a pointer for review, not ground truth
On the same 11,102 analysed frames: both detectors agreed on **4,827** boxes (IoU ≥ 0.3); **115** were YOLO-only; **7,364** were YuNet-only. 98 % of YOLO's detections were also found by an independent detector.
YuNet ran on a 960-px-wide copy at score threshold 0.5. A large YuNet-only count does **not** mean YOLO missed that many faces: manual review (next section) shows most YuNet-only boxes are not faces.

## 5. Manual review of a sample (what a human can see)
**Method and limits.** I (the AI assistant) looked at the annotated evidence frames: 4 contact sheets (24 frames across 6 of the `record_*`
clips) plus the 4K crowd frame. That is **25 frames, one reviewer, no ground-truth annotation** - treat every number below as
indicative, not as accuracy. A proper evaluation needs a human to label a few hundred frames (suggested next step).

| Observation in the reviewed frames | Result |
|---|---|
| Box alignment | Boxes sit tightly on faces/heads; no visible offset or scale error (also verified mathematically: synthetic-video pixel test IoU > 0.6, `docs/phase-1-report.md`) |
| Clear true detections | Walkers facing the camera were found in every reviewed frame where a face was visible, including a child and people at the frame edge (confidence 0.6–0.8) |
| Likely false positives | A handful (≈ 6 of ≈ 40 boxes) were low-confidence (0.50–0.60) boxes on the **back of a head** (people walking away) or on tiny seated figures at the far right edge. **No box was seen on a floor, shoe, bag or other non-head object** |
| Missed detections | In the crowded 4K frame (~24 visible faces) about **3 were missed**: a face wearing a mask at the left edge and a face cut by the bottom edge, and a partly visible head. In the hall footage, very small/far faces and faces seen from behind or at a steep angle are not detected (by design for back views) |
| Where the second detector disagreed | YuNet (threshold 0.5) reported many extra boxes. In the reviewed frames these were mostly **YuNet false positives** (floor tiles, feet, a phone, shirt collars, backs of heads) with a few real masked/edge faces. So "YuNet-only" counts in the table are **not** a count of YOLO misses |

**Takeaway.** On this footage the YOLO11n-face detector looks precise but not complete: it errs toward missing small, masked, cropped or
averted faces, and it occasionally fires on the back of a head at confidence near the 0.5 threshold. Raising the threshold to about 0.6 would
remove most of the questionable boxes at the cost of more misses; do **not** tune it without a labelled set.

## 6. What these numbers are not
* They are **detections in sampled frames**. One person walking through produces dozens of detections; none of the counts here is a visitor
  count, and nothing in the product labels them as such.
* No tracking or identity exists (Phase 2/3), so the same person cannot be recognised across frames or clips.
* The footage appears to be a fixed overhead hall camera; results may not transfer to other views.

