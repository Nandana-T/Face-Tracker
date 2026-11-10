# Phase 1 benchmark (measured)

Generated 2026-11-10 21:08:11 by `backend/benchmarks/benchmark_detector.py` (re-run it to reproduce; this file is rendered by `render_report.py` from the JSON next to it).

> Content is a **synthetic** video made from one public-domain portrait. Latency and throughput of a fixed-size CNN do not depend on image content, but **detection accuracy is not measured here**. Numbers are from a laptop CPU that may throttle; expect run-to-run variation of several percent.

## 1. Environment
| | |
|---|---|
| CPU | 12th Gen Intel(R) Core(TM) i5-1240P - 12 physical cores / 16 logical |
| RAM | 15.7 GB |
| GPU reported by OS | Intel(R) Iris(R) Xe Graphics |
| CUDA usable | **no** (ONNX Runtime providers: AzureExecutionProvider, CPUExecutionProvider) |
| OS / Python | Windows 11 (10.0.26200) / 3.12.16 |
| ONNX Runtime / OpenCV | 1.31.1 / 5.0.0 |

## 2. Model
* `yolov11n-face` - 10.09 MiB on disk, input 1x3x640x640 float32 (letterboxed)
* SHA-256 `2dfe14171f5b76a05f9bcf0dac7f94b7bff4416b1f29eff7c9ef5830f51c5719`
* Initialisation (create the ONNX Runtime session, 7 repeats): mean **84.59 ms**, p95 97.1 ms

## 3. Per-frame latency (flat out, 200 frames after 15 warm-up, ONNX Runtime default spin-wait)
Stages: *pre* = letterbox + RGB/normalise, *inference* = `session.run`, *post* = decode + NMS. CPU = process CPU as % of the whole machine while looping.

| Threads | Source frame | pre ms | inference mean / p50 / p95 ms | post ms | total mean ms | frames/s | CPU % of machine |
|---|---|---|---|---|---|---|---|
| 1 | 960x540 | 5.44 | 84.89 / 84.02 / 92.37 | 0.21 | 90.54 | 11.0 | 6.3 |
| 2 | 960x540 | 6.05 | 55.21 / 53.12 / 71.21 | 0.24 | 61.5 | 16.3 | 12.5 |
| 4 | 960x540 | 6.49 | 41.27 / 39.97 / 50.42 | 0.26 | 48.02 | 20.8 | 24.9 |
| 8 | 960x540 | 7.08 | 32.91 / 32.53 / 36.44 | 0.28 | 40.27 | 24.8 | 50.2 |
| auto | 960x540 | 8.02 | 31.03 / 30.15 / 36.59 | 0.34 | 39.4 | 25.4 | 73.9 |
| auto | 640x360 | 7.83 | 32.01 / 31.22 / 36.23 | 0.34 | 40.18 | 24.9 | 74.2 |
| auto | 1280x720 | 8.12 | 32.61 / 31.76 / 38.01 | 0.34 | 41.07 | 24.3 | 74.3 |
| auto | 1920x1080 | 8.42 | 33.53 / 32.35 / 40.05 | 0.36 | 42.31 | 23.6 | 74.0 |

Reading: the model input is fixed at 640×640, so source resolution only changes the cheap resize (640×360 → 1920×1080 costs about 2 ms more per frame). Threads matter: 1 → 4 threads cuts inference by about half; beyond ~8 threads returns diminish while CPU use keeps growing.

## 4. Whole pipeline on a 40 s / 1000-frame video (reader thread + detector + JPEG encode, flat out)
| frame_skip | detector runs on | wall s | source frames consumed / s | detector runs / s | avg inference ms | CPU % of machine | peak RSS MB | real-time factor (25 fps source) |
|---|---|---|---|---|---|---|---|---|
| 0 | 1000 of 1000 | 43.27 | 23.1 | 23.1 | 32.93 | 74.5 | 239.0 | 0.92× |
| 2 | 334 of 1000 | 16.56 | 60.4 | 20.2 | 37.68 | 73.0 | 240.0 | 2.42× |
| 5 | 167 of 1000 | 8.81 | 113.6 | 19.0 | 37.81 | 74.2 | 239.0 | 4.54× |

Reading: with `frame_skip=0` this CPU manages ≈ 23 frames/s (slightly *below* real time for a 25 fps source); the default `frame_skip=2` needs only ≈ 8.3 detector runs/s and processes the file about 2.4× faster than real time. Memory is flat (≈ 240 MB RSS including the model) - queues are bounded.

## 5. Real-time operation (25 fps source, `frame_skip=2`, paced like a camera) - effect of ORT spin-wait
| ORT threads | spin-wait | avg inference ms | CPU % of machine | peak RSS MB |
|---|---|---|---|---|
| auto | on (ORT default) | 27.98 | 70.9 | 228.0 |
| auto | off | 36.32 | 9.8 | 231.0 |
| 4 | on (ORT default) | 37.56 | 21.4 | 229.0 |
| 4 | off | 39.31 | 6.8 | 230.0 |

**Finding:** ONNX Runtime's worker threads busy-wait between frames. With ORT defaults the worker burned about 70 % of this 16-thread CPU to run ~8 inferences/s. Turning spin-wait off cut that to ≈ 10 % (auto threads) or ≈ 7 % (4 threads) for a few ms of extra latency. The shipped defaults are therefore `detection.cpu_spin_wait=false`, `detection.intra_op_threads=4`.

## 6. Frame delivery latency
Worker publish → API relay → WebSocket client, 166 frames, same host (loopback): mean **6.55 ms**, p50 6.45, p95 10.05, p99 13.93, max 16.58 ms.
This excludes capture, inference, browser JPEG decode and paint, and any real network; across a WAN add the round-trip time plus the JPEG size divided by the uplink rate (one 960 px sample frame was about 23 KB; not measured across content).

## 7. GPU
**No usable GPU: nothing GPU-related was measured.** The machine has an integrated Intel Iris Xe GPU only; the installed ONNX Runtime has no GPU execution provider (no CUDA, no DirectML), and no NVIDIA device is present.

*Unverified estimate, not a measurement:* Ultralytics publishes ≈ 1.5 ms (NVIDIA T4, TensorRT) versus ≈ 56 ms (their reference CPU, ONNX) for the COCO **YOLO11n** detector at 640 px. Those are different weights, a different head and different hardware from this project, so treat "a discrete NVIDIA GPU is likely one to two orders of magnitude faster per inference than this CPU" as a hypothesis to be confirmed on the target GPU (the `CUDAExecutionProvider` or `DmlExecutionProvider` of ONNX Runtime would be the first thing to try; the detector currently pins the CPU provider, so this needs a small, untested code change plus the matching ONNX Runtime package).

## 8. Capacity guidance derived from the measurements above
Using the shipped defaults (4 threads, spin-wait off, `frame_skip=2`, 25 fps source) the worker used ≈ 7 % of this machine (≈ 1.1 logical cores) and ≈ 230 MB RSS for **one** stream. That suggests a few streams per 8-core class machine *before* Phase 2 adds embedding cost - but concurrent streams were **not measured** (Phase 1 runs one session at a time), so this is an extrapolation, not a result.
