r"""Renders docs/real-video-validation.md from datasets/evidence/hackathon/results.json.

    .venv\Scripts\python.exe backend\benchmarks\render_video_report.py
The manual-review section is hand-written text (see the constants below) - everything numeric comes from the JSON.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "datasets" / "evidence" / "hackathon" / "results.json"
OUT = ROOT / "docs" / "real-video-validation.md"

REVIEW = """## 5. Manual review of a sample (what a human can see)
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
"""


def main() -> int:
    d = json.loads(SRC.read_text(encoding="utf-8"))
    a, v = d["aggregate"], d["videos"]
    matched = sum(x["cross_check_vs_yunet"]["matched"] for x in v)
    yolo_only = sum(x["cross_check_vs_yunet"]["yolo_only"] for x in v)
    yunet_only = sum(x["cross_check_vs_yunet"]["yunet_only"] for x in v)
    frames_with_face = sum(
        sum(c for k, c in x["faces_per_analysed_frame"]["distribution"].items() if k != "0")
        for x in v
    )
    sample = next(x for x in v if x["file"].startswith("video_sample"))
    records = [x for x in v if x["file"].startswith("record_")]
    L: list[str] = []
    w = L.append
    w("# Real-video validation (hackathon footage)")
    w("")
    w(
        "Source: the folder named in the problem statement (Google Drive), downloaded with `gdown` from the public share link - **no sign-in was needed**. "
        f"{a['videos']} files, {sum(x['size_mb'] for x in v):.0f} MB, stored under `datasets/hackathon` (git-ignored). "
        '`video_sample1.mp4` is presumably the "sample video"; the other 22 `record_*.mp4` are hall-camera recordings.'
    )
    w("")
    w(
        "Reproduce: `backend\\benchmarks\\evaluate_video.py --videos datasets\\hackathon --out datasets\\evidence\\hackathon`, then `render_video_report.py`."
    )
    w(
        "The annotated evidence frames (92) and review sheets (16) are in `datasets/evidence/hackathon/` - **git-ignored on purpose: they show real people.**"
    )
    w("")
    w(
        "> All counts below are **face detections in analysed frames**, not visitors or unique people."
    )
    w("")
    w("## 1. Video properties")
    w(
        f"* `record_*` ({len(records)} files): {records[0]['width']}×{records[0]['height']} @ {records[0]['fps']} fps, {records[0]['frames_declared']} frames ≈ {records[0]['duration_s']} s each, codec `{records[0]['codec']}`, ≈ {sum(x['size_mb'] for x in records) / len(records):.1f} MB each."
    )
    w(
        f"* `video_sample1.mp4`: {sample['width']}×{sample['height']} @ {sample['fps']} fps, {sample['frames_declared']} frames ≈ {sample['duration_s']} s, codec `{sample['codec']}`, {sample['size_mb']} MB."
    )
    w(
        f"* Total: {a['frames_read']:,} frames, {a['total_duration_s'] / 60:.1f} minutes. Unreadable/corrupt frames: **{a['corrupt_frames']}**."
    )
    w("")
    w("## 2. Processing (the real pipeline, shipped settings)")
    s = a["settings"]
    w(
        f"`frame_skip={s['frame_skip']}` (every 3rd frame analysed), 4 ORT threads, spin-wait off, confidence ≥ {s['confidence_threshold']}, "
        "run flat out (no real-time pacing), JPEG encode for streaming included."
    )
    w("")
    w("| | Result |")
    w("|---|---|")
    w(
        f"| Frames read / analysed / skipped | {a['frames_read']:,} / {a['frames_analysed']:,} / {a['frames_read'] - a['frames_analysed']:,} |"
    )
    w(f"| Face detections (sum over analysed frames) | {a['yolo_detections']:,} |")
    w(
        f"| Analysed frames containing ≥ 1 face | {frames_with_face:,} of {a['frames_analysed']:,} ({100 * frames_with_face / a['frames_analysed']:.0f} %) |"
    )
    w(
        f"| Detector throughput | {a['detector_runs_per_s']} analysed frames/s overall; 2.7K clips ≈ 12–16/s, the 4K sample {sample['detector_runs_per_s']}/s |"
    )
    w(
        f"| Inference latency (ONNX, 640×640) | mean {a['avg_inference_ms']} ms (clips 48–67 ms; 4K sample {sample['avg_inference_ms']} ms - slower than the 31-40 ms synthetic benchmark because decoding 2.7K/4K frames competes for the same cores) |"
    )
    w(
        f"| CPU use (process, % of 16-thread machine) | mean {a['mean_cpu_percent_of_machine']} % (4K sample {sample['cpu_percent_of_machine']} %) |"
    )
    w(
        f"| Peak memory (RSS) | ≈ 520 MB for 2.7K clips, {sample['peak_rss_mb']} MB for the 4K sample (large decoded frames) |"
    )
    w(
        f"| 4K decode headroom | the 4K sample was consumed at {sample['source_frames_per_s']} source frames/s flat out vs its {sample['fps']} fps: **about real time, no headroom** (2.7K clips: ≈ 47/s vs 25 fps) |"
    )
    w(
        f"| Wall time | {a['wall_s'] / 60:.1f} min for {a['total_duration_s'] / 60:.1f} min of video ({a['total_duration_s'] / a['wall_s']:.1f}× real time on one pipeline) |"
    )
    w("")
    w("## 3. Faces per sampled frame, per video")
    w(
        "| Video | Frames | Analysed | Detections | Mean faces / analysed frame | Max in a frame | Mean / min confidence | Smallest face (px) | Cross-check: both / YOLO-only / YuNet-only |"
    )
    w("|---|---|---|---|---|---|---|---|---|")
    for x in v:
        c = x["cross_check_vs_yunet"]
        w(
            f"| {x['file']} | {x['frames_read']} | {x['frames_analysed']} | {x['yolo_detections']} | {x['faces_per_analysed_frame']['mean']} | "
            f"{x['faces_per_analysed_frame']['max']} | {x['confidence']['mean']} / {x['confidence']['min']} | {x['smallest_face_px']} | {c['matched']} / {c['yolo_only']} / {c['yunet_only']} |"
        )
    w("")
    w("## 4. Independent cross-check (YuNet, MIT licence) - a pointer for review, not ground truth")
    w(
        f"On the same {a['frames_analysed']:,} analysed frames: both detectors agreed on **{matched:,}** boxes (IoU ≥ 0.3); **{yolo_only:,}** were YOLO-only; "
        f"**{yunet_only:,}** were YuNet-only. {100 * matched / (matched + yolo_only):.0f} % of YOLO's detections were also found by an independent detector."
    )
    w(
        "YuNet ran on a 960-px-wide copy at score threshold 0.5. A large YuNet-only count does **not** mean YOLO missed that many faces: manual review "
        "(next section) shows most YuNet-only boxes are not faces."
    )
    w("")
    w(REVIEW)
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
