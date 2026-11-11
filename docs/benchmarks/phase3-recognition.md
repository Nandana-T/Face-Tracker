# Phase 3 recognition benchmark

Machine: Windows 11, Intel i5-1240P, CPU only (no GPU), ONNX Runtime / OpenCV DNN, shipped `config/config.json` recognition defaults, measured 2026-11-11.
Model: **SFace** `face_recognition_sface_2021dec.onnx` (38.7 MB, 128-d) + **YuNet** landmarks (0.23 MB). Licensing and limits: [ADR 0013](../decisions/0013-recognition-model-and-licensing.md) (hackathon / non-commercial scope; training data of the SFace file undocumented).
Reproduce: `backend\benchmarks\evaluate_recognition.py embed|report` (LFW), `evaluate_identity.py` (identity simulation), `evaluate_recognition_video.py run|ab` (hackathon footage).

**Three kinds of evidence – never mix them**

| Kind | What it is | What it can and cannot tell you |
|---|---|---|
| **Labelled benchmark** | LFW verification pairs (10 folds, 3,000 genuine + 3,000 impostor pairs), identities known | Error rates of *this model and pipeline* on posed press photos. Optimistic for surveillance footage |
| **Controlled simulation** | 300 distinct LFW people through the real `IdentityManager` (several observations of one photo, then a *different* photo of the same person) | Registration / recognition / duplicate / wrong-identity rates of the decision logic at registry size ≈ 300. Still still-photos |
| **Real-video qualitative** | The 6-clip hackathon subset through the real pipeline; saved crops reviewed by eye (one AI reviewer) | Quality-gate behaviour, how many faces get decided, cost. **No identity labels exist: no accuracy figure is derived** |
| Synthetic pipeline tests | Unit/integration tests with fabricated embeddings and the public-domain portrait | Logic and wiring only |

## 1. Verification on LFW (labelled)
Full protocol, `pad 0.6`: 7,695 of 7,701 images processed (1 no face, 5 no landmarks); 2,993 genuine + 2,996 impostor pairs scored (7 + 4 pairs had an unprocessable image – counted, not hidden).

| | mean | p1 | p5 | p25 | p50 | p75 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|---|---|---|---|---|---|
| genuine similarity | 0.657 | 0.327 | 0.456 | 0.591 | 0.668 | 0.736 | 0.826 | | | |
| impostor similarity | 0.086 | | −0.059 | 0.024 | 0.084 | 0.147 | 0.233 | 0.293 | 0.348 | **0.393** |

Best single-threshold pair accuracy **99.4 %** at 0.34 (the figure published for SFace is 99.4 %, so the pipeline reproduces the model).

| Operating point | threshold | FMR (false match) | FNMR (false non-match) |
|---|---|---|---|
| FMR ≤ 1 % | 0.30 | 0.80 % | 0.74 % |
| FMR ≤ 0.1 % | 0.38 | 0.07 % | 2.0 % |
| no impostor pair above | 0.40 | 0 (of 2,996) | 2.5 % |
| FNMR ≤ 5 % | 0.45 | 0 | 4.5 % |
FMR below ≈ 1/3,000 cannot be resolved here. Landmark crop padding (0.35 / 0.6 / 1.0) changed pair accuracy by < 0.2 % (99.5 / 99.3 / 99.5 on fold 1) – 0.6 kept.

## 2. Does it survive small and blurry faces? (LFW degraded, 2 folds ≈ 1,200 pairs each)
Clean downscaling (face width in pixels → pair accuracy, FNMR at the match threshold 0.50): 97 px → 99.6 % / 6.8 %; 68 px → 99.7 % / 6.7 %; 48 px → 99.8 % / 8.2 %; **34 px → 99.7 % / 10.7 %**. Impostor maximum stayed ≤ 0.335. So small *clean* faces are mostly fine for this model.

Defocus blur (Gaussian, applied to the 68 px version; "sharpness" = Laplacian variance of the 112×112 aligned crop, the quality-gate metric):

| blur σ | median sharpness | images that still process | pair accuracy | FNMR @ 0.50 | FNMR @ 0.40 | impostor p99 / max | FMR @ 0.40 |
|---|---|---|---|---|---|---|---|
| none | 92 | 99.6 % | 99.7 % | 6.7 % | 1.5 % | 0.27 / 0.32 | 0 |
| 1.5 | **15** | 99.6 % | 98.7 % | 16.4 % | 5.2 % | 0.29 / **0.36** | 0 |
| 2.5 | 7 | 81 % | 96.2 % | 30.7 % | 12.1 % | 0.34 / 0.38 | 0 |
| 3.5 | 5 | 41 % | 91.1 % | 42.7 % | 20.6 % | 0.41 / 0.46 | **2.8 %** (n = 106) |
**Consequence for the quality gate:** impostor similarities creep up as faces get blurrier and cross the 0.40 novelty threshold at sharpness ≈ 5. At sharpness ≈ 15 they are still below 0.36, so `min_sharpness` was set to **15**
(it was 25 before this study and before seeing the footage). This is a calibration on synthetic blur: real motion blur and codec artefacts may behave differently; the effect on duplicates/merges in real footage cannot be measured without labels.
The pitch ratio range was also corrected from a wrong guess (0.45–1.25, which rejected 55 % of ordinary frontal faces) to the measured frontal LFW range (5–95 % = 1.0–1.62 → allowed 0.75–1.95).

## 3. Identity-level simulation: duplicates and wrong merges (300 people, controlled)
Each person appears twice: session 1 = one photo (5 observations: 1 original + 4 jittered copies through the real pipeline), session 2 = a **different photo** of the same person. Real `IdentityManager`, 300-visitor registry at the end.
Rates are per person (session 1) or per known returning person (session 2). *Wrong identity / wrong merge = a track assigned to someone else's visitor – the dangerous error.*

| novelty / match threshold | registered on first appearance | left undecided | wrong merge (s1) | known people recognised (s2) | **wrong identity (s2)** | duplicate of a known person (s2) |
|---|---|---|---|---|---|---|
| 0.30 / 0.40 | 45.7 % | 52.3 % | 2.0 % | 95.6 % | 3.7 % | 0 |
| 0.30 / 0.45 | 45.7 % | 54.3 % | 0 | 93.4 % | 0 | 0 |
| 0.30 / 0.50 | 45.7 % | 54.3 % | 0 | 92.0 % | 0 | 0 |
| 0.35 / 0.40 | 72.0 % | 24.3 % | 3.7 % | 95.8 % | 3.0 % | 0.9 % |
| 0.35 / 0.45 | 72.0 % | 28.0 % | 0 | 93.5 % | 0.3 % | 0.9 % |
| 0.35 / 0.50 | 72.0 % | 28.0 % | 0 | 92.6 % | 0 | 0.9 % |
| 0.40 / 0.40 | 92.0 % | 4.0 % | 4.0 % | 96.0 % | 1.3 % | 1.1 % |
| **0.40 / 0.50 (chosen)** | **92.0 %** | 8.0 % | **0** | 92.4 % | **0** | **1.1 %** (3 of 297) |
| 0.40 / 0.45 | 92.0 % | 8.0 % | 0 | 94.2 % | 0.33 % | 1.1 % |
**What this shows:** (1) match threshold < 0.45 produces wrong merges; (2) a **per-pair** impostor tail of 0.29 does not scale to a registry: against hundreds of visitors almost every new face has *some* visitor ≥ 0.30, so a low novelty threshold leaves
half of the people undecided forever (a 1:N effect – thresholds from pair statistics alone would have been wrong); (3) 0.40 / 0.50 registers 92 % of new people at the cost of ≈ 1 % duplicates and zero wrong identities in this test.
Larger registries raise the wrong-identity risk (more chances for a chance match), and real footage is harder than LFW.

## 4. Real footage (hackathon clips; **no identity labels – not an accuracy measurement**)
Pipeline: YOLO → tracker → recognition thread → identity manager, `frame_skip=2`, six clips through one registry in sequence. "Tracks" = confirmed tracks that produced at least one observation.

| Clip | Tracks | Faces analysed / usable | Final track states | New visitors | Registry after |
|---|---|---|---|---|---|
| 183903 hall | 12 | 52 / 18 | 3 registered · 8 low quality · 1 rejected | 3 | 3 |
| 184807 hall | 15 | 66 / 31 | 2 registered · 2 pending · 8 low quality · 3 rejected | 2 | 5 |
| 185210 hall | 5 | 33 / 14 | 1 pending · 1 unresolved · 2 low quality · 1 rejected | 0 | 5 |
| 184403 hall | 7 | 26 / 8 | 1 registered · 5 low quality · 1 rejected | 1 | 6 |
| 190324 hall | 5 | 25 / 5 | 1 registered · 1 pending · 3 low quality | 1 | 7 |
| sample1 crowded 4K | 30 | 139 / 32 | 2 registered · 6 pending · 21 low quality · 1 unresolved | 2 | 9 |

* **Most faces are *not* used**, by design: reasons seen across clips – blurry (motion/defocus), turned away (profile), too small (< 40 px, mostly in the 2.7K hall clips), detector confidence < 0.6, looking down (top-down camera), masks/hands. The crowd clip's faces are 60–119 px wide, so size is not what limits it there; motion blur and head pose are.
  Reviewing saved crops of both rejected and accepted faces, the rejections of profile and visibly soft faces looked right; the 15–25 sharpness band is borderline (hence the study in §2).
* **9 visitors from 6 clips.** Visual review of the 9 saved representative crops: they look like nine different people – no duplicate was visible. One is a masked face, one is visibly soft (a consequence of the relaxed sharpness limit). Whether the same person walking through several clips was *re-recognised* correctly is **unknown**: no cross-clip recognition event occurred, and nothing can verify it without labels.
* **Rejected as inconsistent: 7 tracks** (the observations of one track disagreed with each other → no identity was created) and 7 ambiguous notifications – the safeguards are active on real data.
* Behaviour without labels: duplicates, missed registrations and wrong merges **cannot be quantified here**. Frames and crops with real people are in `datasets/evidence/recognition/video/` (git-ignored).

## 5. Cost (measured)
| Item | Result |
|---|---|
| Model load (embedder + landmark model, warm file cache) | 0.11 s (once per processing session) |
| Per face: crop → landmarks → align → quality → embedding (recognition thread) | mean 10–16 ms, p95 31–38 ms (standalone embedding ≈ 22 ms) |
| Identity lookup (exact cosine over the in-memory index, 27 embeddings) | 0.07 ms mean |
| Registration transaction (SQLite in this benchmark; image write + DB commit) | 17–27 ms mean |
| Faces dropped because the recognition queue was full | 0 in all clips |
End-to-end A/B on the real pipeline (first 600 source frames, `frame_skip=2`, median of 3 interleaved runs; tracking on in both):

| Clip | | wall s | process CPU s | peak RSS MB | detector ms/frame |
|---|---|---|---|---|---|
| hall 183903 | without recognition | 14.6 | 54.2 | 491 | 60.3 |
| | with recognition | 15.2 (+4 %) | 57.4 (+6 %) | 547 (+56) | 62.4 |
| crowded 4K | without | 7.8 | 39.9 | 849 | 76.8 |
| | with | 8.0 (+3 %) | 48.3 (**+21 %**) | 913 (+64) | 78.2 |
Run ranges do not overlap here (hall CPU 53.9–55.9 vs 57.2–58.3 s; 4K 37.5–40.5 vs 47.8–50.4 s), so these differences are real. Recognition runs on its own thread behind a bounded queue and is not
computed for every face on every frame (warm-up every 0.5 s per track, then every 5 s, within an 80 ms/frame budget); it costs the detector ≈ 2 ms/frame through shared CPU. Not measured: GPU, real RTSP, PostgreSQL registration latency under load, memory growth over hours.
