/** Hand-maintained mirror of backend/app/schemas/system.py. See docs/api-contracts.md. */

export interface HealthResponse {
  status: "ok";
  service: string;
}

export interface DependencyCheck {
  status: "ok" | "unavailable";
  latency_ms: number | null;
  error: string | null;
}

export interface ReadinessResponse {
  status: "ready" | "not_ready";
  checks: { database: DependencyCheck };
}

export type ComponentState =
  | "not_implemented"
  | "not_configured"
  | "not_running"
  | "ready"
  | "unavailable"
  | "active";

export interface ComponentStatus {
  state: ComponentState;
  detail: string;
}

export interface SystemStatusResponse {
  service: string;
  version: string;
  environment: string;
  uptime_seconds: number;
  database: {
    status: "ok" | "unavailable";
    migration_revision: string | null;
    migrations_applied: boolean;
  };
  components: {
    video_worker: ComponentStatus;
    face_detector: ComponentStatus;
    face_recognizer: ComponentStatus;
    tracker: ComponentStatus;
  };
  active_video_sources: number;
  video_source_configured: boolean;
}

export interface PublicConfigResponse {
  environment: string;
  video: {
    source_type: "file" | "rtsp";
    source_configured: boolean;
    reconnect_interval_seconds: number;
  };
  detection: { frame_skip: number; confidence_threshold: number; model: string; input_size: number };
  stream: { max_fps: number; max_width: number };
  recognition: { similarity_threshold: number };
  tracking: { max_missing_seconds: number };
  logging: { level: string };
}

export interface ErrorEnvelope {
  error: { code: string; message: string; request_id: string | null; details: unknown };
}

// ---- Phase 1: sources, sessions, live stream ------------------------------------------------

export type SessionState =
  | "IDLE"
  | "STARTING"
  | "RUNNING"
  | "STOPPING"
  | "STOPPED"
  | "COMPLETED"
  | "ERROR";

export const ACTIVE_STATES: readonly SessionState[] = ["STARTING", "RUNNING", "STOPPING"];

export interface SourceOut {
  id: string;
  label: string;
  type: "file" | "rtsp";
  availability: "available" | "unavailable";
  detail: string | null;
}

export interface WorkerOut {
  connected: boolean;
  worker_id: string | null;
  model_available: boolean | null;
  model_detail: string | null;
}

export interface SourcesResponse {
  sources: SourceOut[];
  worker: WorkerOut;
}

/** Detector/ingestion counters. `detections_total` sums faces over processed frames - it is NOT a
 * number of distinct people. */
export interface SessionStats {
  frames_read: number;
  frames_processed: number;
  frames_skipped: number;
  frames_dropped: number;
  corrupt_frames: number;
  detections_total: number;
  faces_last_frame: number;
  max_faces_in_frame: number;
  last_inference_ms: number;
  avg_inference_ms: number;
  processing_fps: number;
  last_frame_id: number;
  frame_width: number;
  frame_height: number;
  source_fps: number;
  elapsed_seconds: number;
}

export interface SessionOut {
  id: string;
  state: SessionState;
  source: SourceOut | null;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  stop_reason: string | null;
  error: { code: string; message: string } | null;
  connection: string | null;
  stats: SessionStats;
  settings: { model: string; frame_skip: number; confidence_threshold: number } | null;
}

/** Header of one binary stream message (see backend app/schemas/detection.py).
 * `bbox` = [x1, y1, x2, y2] integer pixels in the ORIGINAL frame (frame_width x frame_height). */
export interface FrameHeader {
  session_id: string;
  frame_id: number;
  timestamp_ms: number;
  frame_width: number;
  frame_height: number;
  faces: { bbox: [number, number, number, number]; confidence: number }[];
  inference_ms: number;
  /** Worker wall-clock publish time (epoch ms); for latency measurement only. */
  published_at_ms?: number;
}

export type StreamMessage =
  | { type: "snapshot" | "state" | "ended"; session: SessionOut }
  | { type: "stats"; stats: SessionStats };
