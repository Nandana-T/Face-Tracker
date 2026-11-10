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

export type ComponentState = "not_implemented" | "not_configured" | "not_running";

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
  detection: { frame_skip: number; confidence_threshold: number };
  recognition: { similarity_threshold: number };
  tracking: { max_missing_seconds: number };
  logging: { level: string };
}

export interface ErrorEnvelope {
  error: { code: string; message: string; request_id: string | null; details: unknown };
}
