# ADR 0013 – Face recognition model, alignment and licensing

Status: accepted for the **hackathon / non-commercial** scope (Phase 3). Not cleared for commercial use or for redistribution of weights.

## Context
Phase 3 needs a face-embedding model that runs on CPU through ONNX Runtime (no heavyweight framework), takes landmark-aligned 112×112 crops, and whose
weights we may legitimately download and use. The Python package licence and the pretrained-weight licence are different things; the weights' own terms
and their training data decide what we may do.

## What was checked (this session, from the primary sources)
| Candidate | Weights licence (as stated by the publisher) | Training data stated | CPU/size | Verdict |
|---|---|---|---|---|
| **SFace `face_recognition_sface_2021dec.onnx`** (OpenCV Zoo, MobileFaceNet + SFace loss) | OpenCV Zoo README: *"All files in this directory are licensed under Apache 2.0 License"* (a LICENSE file sits in the directory). The Zoo root README adds that the licences of individual models should be checked | **Not stated by the Zoo.** The SFace paper reports experiments on CASIA-WebFace, VGGFace2 and MS-Celeb-1M – datasets whose own terms are research-oriented, and the pretrained file's exact training set is **not documented** | 38.7 MB fp32, 128-d, ≈ 22 ms/face (i5-1240P) | **Selected** – file licence is explicit and permissive; training-data provenance unresolved |
| InsightFace `buffalo_l` (`w600k_r50`, ArcFace R50, 512-d) | InsightFace model zoo / package docs: *"all models are available for non-commercial research purposes only"*; only the library **code** is MIT. Commercial packs exist from the vendor (contact required) | WebFace600K (annotation/data restrictions inherited) | ≈ 167 MB, slower | Documented for non-commercial research use, so it would also be acceptable for the hackathon; **not downloaded** (SFace was sufficient and its file licence is clearer). Registry entry intentionally absent: nothing unverified is pinned |
| YuNet `face_detection_yunet_2023mar.onnx` (landmarks only) | MIT (OpenCV Zoo / libfacedetection) | not stated | 0.23 MB | Used **only** to obtain the 5 alignment landmarks |

Nothing legally ambiguous was downloaded silently: both files above are fetched by an explicit command (`python -m app.vision.recognition.registry fetch`,
`scripts\download-model.ps1 -Recognition`) with the URL, size and SHA-256 pinned in `app/vision/recognition/registry.py`, stored under `models/face-recognizer/` on D:,
and verified before every load (worker start-up reports a generic "model missing/corrupt" otherwise).

## Decision
* **Embedder:** SFace (OpenCV Zoo), ONNX Runtime CPU, input 112×112 **RGB** 0–255 float32 NCHW (verified against OpenCV's reference `FaceRecognizerSF`: cosine 1.0;
  feeding BGR gives cosine ≈ 0.95 and is wrong), output 128-d, L2-normalised and validated (finite, non-zero norm, expected dimension).
* **Alignment:** YuNet 5-point landmarks computed on a padded crop around the (trusted) YOLO box → similarity transform onto the standard ArcFace template (identical to OpenCV's `alignCrop`,
  mean pixel difference 0.0005). The YOLO model in this project outputs boxes only, so landmarks must come from somewhere; running the 0.23 MB YuNet on a small crop is cheap.
* **Model identity in the data:** every stored embedding records its `model_version`; vectors of different models are never compared (separate index partitions, tested).
* Models are loaded in the **worker** (never in the FastAPI process or event loop). The API only receives embeddings as numbers.

## Licence consequences (recorded, not resolved)
1. **Hackathon, local use, weights downloaded by the user:** acceptable.
2. **Commercial deployment or redistribution of the weights/derived embeddings:** *not cleared.* The SFace file is Apache-2.0 by declaration, but its training data is undocumented and is very likely
   drawn from research-licensed face datasets; this must be clarified with the model's authors/OpenCV before any commercial use. InsightFace's weights are explicitly non-commercial.
3. The repository does not contain model weights (`models/` is git-ignored); the README tells users to fetch them explicitly.
4. Replacing the model later is a registry + alignment-preprocessing change; stored embeddings of the old model stay in their own partition and are never matched against new vectors (a re-enrolment or migration job would be needed).

## Consequences
* Accuracy is that of a small MobileFaceNet model: LFW pair accuracy measured here **99.4 %** (the published figure is 99.4 %), but it is weaker than R50/R100 ArcFace models on hard, low-resolution, non-frontal surveillance faces.
  Thresholds therefore are deliberately conservative (docs/phase-3-report.md).
* Face recognition has inherent error rates and demographic performance differences; the system never infers age, gender or ethnicity and must not be used for decisions about people without human review.
