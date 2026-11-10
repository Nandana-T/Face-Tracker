# ADR 0009 – Frame delivery, frame skipping and back-pressure

**Status:** accepted (Phase 1).

## Frame skipping
`detection.frame_skip = N` means **N frames are skipped between detections**: the detector runs on frame indices
`0, N+1, 2(N+1), …` (N=0 → every frame; N=2 → every third frame). Skipped frames are counted
(`frames_skipped`) but **no detection is carried over or interpolated** - boxes simply do not exist for those
frames. Presenting skipped frames with old boxes would be tracking, which is Phase 3.

## What is streamed
Only frames the detector ran on are published (so every picture has *its own* boxes), throttled to `stream.max_fps`
(15) and down-scaled to `stream.max_width` (960 px) JPEG q70. A message carries the detector result in **original-frame
pixel coordinates** plus `frame_width/height`; the browser maps them through the rectangle the frame actually occupies
on the canvas (letterboxing handled in `frontend/src/lib/geometry.ts`). No frame is ever written to PostgreSQL or disk.

## Back-pressure inside the worker
```
reader thread ──bounded queue(worker.queue_size)──► inference thread ──latest-wins slot──► WebSocket sender
```
* Live sources (RTSP): queue full ⇒ drop the **oldest** frame (`frames_dropped`); latency beats completeness.
* Files: the reader **blocks** (nothing dropped); with `worker.realtime_pacing` it also sleeps to the source rate.
* Memory is therefore bounded by `queue_size` frames + one encoded frame, regardless of inference speed.

## Browser
`LiveStream` decodes with `createImageBitmap`, latest-wins (a newer frame replaces a waiting one), and the canvas is
painted imperatively on `requestAnimationFrame` - **no React render per frame**. React state is refreshed from the
newest header every 400 ms for the numeric readouts.

## Timestamps and indices
`frame_id`: 0-based among frames delivered by the source (undecodable frames consume no index). `timestamp_ms`: files
`frame_id / fps · 1000`; live streams monotonic ms since open. Variable-frame-rate files are approximated by the
nominal fps (documented limitation).
