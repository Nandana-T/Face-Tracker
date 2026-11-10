# ADR 0011 – Licence assessment of the YOLO11n-face weights (supersedes the licence notes in ADR 0007)

**Status:** accepted for hackathon/internal, non-commercial use; **not cleared for commercial use**. The model was **not** changed.
This is an engineering assessment from public sources, not legal advice.

## What was inspected (2026-11-10)
| Source | Finding |
|---|---|
| Hugging Face model card `AdamCodd/YOLOv11n-face-detection` (rev `0e97fea5…`) | Metadata `license: apache-2.0`; `base_model: Ultralytics/YOLO11`; "trained for 225 epochs on the WIDERFACE dataset". No licence text, no dataset-terms statement, no intended-use or restriction section. Repo files: `model.pt`, `model.onnx` (ours), `model_fp16.onnx`, `model_gpu.onnx`, `model_nms.onnx` |
| Metadata embedded in the ONNX file we use | `author: Ultralytics`, `version: 8.3.40`, **`license: AGPL-3.0 License (https://ultralytics.com/license)`**, `task: detect`, trained "on /kaggle/working/yolo_dataset/dataset.yaml" |
| Ultralytics licence page | "All Ultralytics YOLO trained models fall under the AGPL-3.0 License by default"; it "covers the training code and the models produced by that training code"; applies even when training from scratch or using YOLO only internally; "custom-trained or fine-tuned YOLO models in a proprietary or commercial setting" require the Enterprise License |
| AGPL-3.0 text | §13: modified versions offered to users over a network must offer them the corresponding source; §6: conveying object code requires the Corresponding Source |
| WIDER FACE (training data) | The Hugging Face dataset card lists `cc-by-nc-nd-4.0` (non-commercial, no derivatives); images come from the public WIDER dataset. The official site's certificate is invalid, so the original terms could not be read directly - **unverified** |

## Assessment
1. **The Apache-2.0 tag is not reliable.** It is set by a third party; the file's own metadata and Ultralytics' stated position say the
   weights are AGPL-3.0, and a re-licence of a derivative of AGPL tooling/weights is not something the third party can grant. The two
   statements conflict; the more restrictive one must be assumed.
2. **Dataset terms add a second, independent uncertainty.** Whether a detector trained on CC BY-NC-ND data is a "derivative" is legally
   contested, but the non-commercial intent is clear. Every WIDER FACE-trained model (including the MIT-licensed YuNet) carries this question.
3. **What we actually do:** the repository contains **no weights** (`models/` is git-ignored); each user downloads the file from
   Hugging Face with `scripts\download-model.ps1`, which pins the exact revision and checksum. Our code does not import Ultralytics - it runs
   the ONNX file in ONNX Runtime. We do not modify or redistribute the weights, and the hackathon use is non-commercial.

## Decision
* **Hackathon / internal demo / evaluation: acceptable.** Downloading the weights for that purpose from the original host and running
  them locally is within what AGPL-3.0 and the dataset's non-commercial terms contemplate. Do **not** bundle the weights in a public
  repository, Docker image, release or hosted service for third parties.
* **Anything commercial, a public SaaS, or a product that ships the weights: not cleared.** Either obtain an Ultralytics Enterprise
  licence (which does not solve the dataset question), or replace the detector.
* **No silent change.** The shipped detector stays YOLO11n-face until the owner decides.

## Recommended replacement if clearance is needed: YuNet (OpenCV Zoo)
| | YOLO11n-face (current) | YuNet `face_detection_yunet_2023mar.onnx` |
|---|---|---|
| Licence of weights/code | AGPL-3.0 (tag says Apache-2.0) | **MIT** ("All files in this directory are licensed under MIT", OpenCV Zoo) |
| Training data | WIDER FACE (card) | not named in the model README (training repo: libfacedetection.train); evaluated on WIDER FACE - **verify before relying on it** |
| Size / runtime | 10.1 MiB, ONNX Runtime | 0.23 MiB, OpenCV `FaceDetectorYN` (CPU) |
| Reported WIDER FACE val AP (easy/med/hard) | 0.942 / 0.921 / 0.810 (model card) | 0.834 / 0.824 / 0.708 (README intro) or 0.884 / 0.866 / 0.750 (README eval table) |

The AP figures use different protocols and are **not comparable**. A measured cross-check on the hackathon footage (agreement between the two
detectors on the same frames) is in `docs/real-video-validation.md`. Switching would be a new `FaceDetector` implementation plus a registry
entry (a small change); it needs the owner's approval and a re-run of the validation. For Phase 2 note that InsightFace's pretrained
recognition models are published for non-commercial research use, so the licensing question will recur there - decide the commercial
stance before Phase 2.
