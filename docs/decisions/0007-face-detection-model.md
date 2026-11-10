# ADR 0007 – Face detector: YOLO11n-face (WIDERFACE) on ONNX Runtime

**Status:** accepted (Phase 1). Replaceable behind `FaceDetector` (`backend/app/vision/detection/interfaces.py`).

## Decision
`AdamCodd/YOLOv11n-face-detection`, file `model.onnx`, Hugging Face revision
`0e97fea5eacb1460b35725c929d813c7095841b5`, 10 583 429 bytes, SHA-256
`2dfe14171f5b76a05f9bcf0dac7f94b7bff4416b1f29eff7c9ef5830f51c5719`, executed with **ONNX Runtime (CPU)**.
Pinned in `app/vision/detection/model_registry.py`; fetched only by `scripts\download-model.ps1`
(checksum-verified, never at API/worker start-up).

## Why
| Criterion | YOLO11n-face (chosen) | Other candidates considered |
|---|---|---|
| Face-specific training | single class `face`, trained 225 epochs on WIDERFACE; reported val AP easy 0.942 / medium 0.921 / hard 0.810 (model card, unverified by us) | generic COCO YOLO weights have no face class - rejected as the brief requires; YOLOv8n-face community checkpoints are similar but older |
| CPU cost | nano network, 10.6 MB; measured latency in `docs/benchmarks/phase1-benchmark.md` | larger s/m variants cost several times more per frame; SCRFD (InsightFace) is lighter but is not YOLO |
| Install reproducibility | one pinned file + checksum; runtime needs only `onnxruntime` (~13 MB wheel) - **no PyTorch (~hundreds of MB)** | `.pt` weights need `ultralytics` + `torch` |
| Python compatibility | ORT 1.31 / Python 3.12 verified; ONNX opset exported by Ultralytics 8.3.40 | - |
| Future recognition | Phase 2 can use InsightFace (ArcFace) embeddings on the *crops* this detector produces; detector and embedder are independent behind interfaces | - |

## Input / output contract
* Input `images`: `1×3×640×640` float32, RGB, `/255`, **letterboxed** (aspect preserved, grey 114 padding, centred).
  The ONNX graph is static 640×640, so `detection.input_size` must be 640 (validated at startup).
* Output `output0`: `1×5×8400` = `cx, cy, w, h, face_confidence` in letterboxed pixels (sigmoid already applied; no
  objectness). Post-processing: inclusive confidence threshold → map back through the letterbox → clip to the frame →
  drop sub-pixel boxes → OpenCV NMS (`iou_threshold`) → cap at `max_faces`.

## Licence - read before commercial use
> **Superseded by [ADR 0011](0011-model-licence-assessment.md)**, which records the full inspection and the decision. Summary: treat as AGPL-3.0 / non-commercial.
* The Hugging Face model card states `license: apache-2.0`.
* The ONNX file's own metadata says `AGPL-3.0` (Ultralytics), and the architecture/export tooling is Ultralytics
  YOLO11 (AGPL-3.0, with a commercial licence available).
* The training data (WIDERFACE) has its own terms (non-commercial research on the dataset page).

**These statements conflict. Until counsel resolves it, treat the weights as AGPL-3.0 and non-commercial.** For a
hackathon/internal prototype that is acceptable; before any commercial or network-service deployment either obtain an
Ultralytics enterprise licence, or swap in a detector with unambiguous terms (the interface makes that a one-file change).

## Limits
Best on frontal/slightly angled faces ≥ ~20 px; accuracy was **not** measured by us on any real footage.
