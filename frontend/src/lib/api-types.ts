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
  tracking: { enabled?: boolean; max_missing_seconds: number; confirm_hits?: number };
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
  recognition_available?: boolean | null;
  recognition_detail?: string | null;
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
  /** Phase 2 tracking counters (temporary session-scoped tracks - NOT visitors). */
  tracks_enabled?: number;
  tracks_confirmed?: number;
  tracks_tentative?: number;
  tracks_lost?: number;
  tracks_created_total?: number;
  tracks_confirmed_total?: number;
  tracks_rejected_total?: number;
  tracks_removed_total?: number;
  tracks_recovered_total?: number;
  tracks_errors?: number;
  tracking_ms_last?: number;
  tracking_ms_avg?: number;
  /** Phase 3 recognition counters (worker side; never embeddings). */
  recog_enabled?: number;
  recog_submitted?: number;
  recog_dropped?: number;
  recog_processed?: number;
  recog_usable?: number;
  recog_errors?: number;
  recog_avg_ms?: number;
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
  settings: {
    model: string;
    frame_skip: number;
    confidence_threshold: number;
    tracking_enabled?: boolean;
    recognition_enabled?: boolean;
  } | null;
}

export type TrackState = "TENTATIVE" | "CONFIRMED" | "LOST";

/** A temporary, session-scoped track. `track_id` is NOT a visitor identity. */
export interface Track {
  track_id: number;
  state: TrackState;
  bbox: [number, number, number, number];
  /** true = a detection was matched in this frame; false = the box is a motion prediction. */
  observed: boolean;
  /** Null for predicted boxes (never invented). */
  detection_confidence: number | null;
  last_observed_confidence: number | null;
  first_seen_ms: number;
  last_seen_ms: number;
  age_ms: number;
  hits: number;
  missed_updates: number;
  last_update_frame: number;
}

export interface TrackingSummary {
  schema_version: number;
  enabled: boolean;
  tentative: number;
  confirmed: number;
  lost: number;
  created_total: number;
  confirmed_total: number;
  tracking_ms: number;
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
  /** Phase 2: the complete current set of TEMPORARY tracks for this session (absent = tracking off). */
  tracks?: Track[];
  tracking?: TrackingSummary | null;
  /** Worker wall-clock publish time (epoch ms); for latency measurement only. */
  published_at_ms?: number;
}

/** What recognition concluded about ONE temporary track (ids are session-scoped; the visitor is persistent). */
export type IdentityState =
  | "UNRESOLVED"
  | "INSUFFICIENT_QUALITY"
  | "MATCH_PENDING"
  | "RECOGNIZED"
  | "AMBIGUOUS"
  | "REGISTRATION_PENDING"
  | "REGISTERED"
  | "REJECTED";

export interface IdentityEventMessage {
  type: "identity";
  session_id: string;
  track_id: number;
  state: IdentityState;
  /** Transition worth a notification (null/"state" = silent state change). */
  event: "state" | "registered" | "recognized" | "ambiguous" | "failed" | "conflict";
  visitor: { id: string; number: number; label: string } | null;
  similarity: number | null;
  reason: string | null;
}

export type StreamMessage =
  | { type: "snapshot" | "state" | "ended"; session: SessionOut }
  | { type: "stats"; stats: SessionStats }
  | IdentityEventMessage
  | { type: "identities"; identities: IdentityEventMessage[] }
  | VisitEventMessage
  | VisitStateMessage
  | VisitsSnapshotMessage;

export interface TrackIdentity {
  state: IdentityState;
  visitor: IdentityEventMessage["visitor"];
}

export interface Visitor {
  id: string;
  number: number;
  label: string;
  status: string;
  first_seen_at: string;
  last_seen_at: string;
  recognition_count: number;
  has_image: boolean;
  model_version: string;
}

export interface VisitorList {
  items: Visitor[];
  total: number;
  limit: number;
  offset: number;
}

export interface VisitorAssociation {
  session_id: string;
  source_label: string | null;
  /** Temporary, only meaningful inside `session_id`. */
  track_id: number;
  state: IdentityState;
  best_similarity: number | null;
  observations: number;
  conflicts: number;
  registered_in_session: boolean;
  first_seen_at: string;
  last_seen_at: string;
}

export interface VisitorDetail extends Visitor {
  first_session_id: string | null;
  image_quality: number | null;
  sessions_count: number;
  tracks_count: number;
  associations: VisitorAssociation[];
}

export type VisitorSort = "last_seen" | "first_seen" | "number";

export interface RecognitionStatus {
  enabled: boolean;
  model: string;
  model_version: string | null;
  worker_available: boolean | null;
  worker_detail: string | null;
  registered_visitors: number;
  indexed_embeddings: number;
}

// ---------------------------------------------------------------------------------------------
// Phase 4 - visit lifecycle. Three identifiers are never interchangeable: a TRACK id (temporary,
// session-scoped, not part of these records), a VISITOR (persistent identity) and a VISIT (one
// presence episode - `visit_id`). A visitor can have many visits.
// ---------------------------------------------------------------------------------------------
export type EventType = "entry" | "exit" | "interrupted";
export type ImageStatus = "PENDING" | "STORED" | "UNAVAILABLE" | "MISSING" | "ERASED";
export type PersistenceStatus = "PENDING" | "DELIVERED" | "DEAD";
export type ExitBasis = "absence" | "observation_ended" | "interrupted";
export type VisitState = "ACTIVE" | "TEMPORARILY_MISSING" | "EXIT_PENDING" | "CLOSED" | "INTERRUPTED";
export type Integrity = "open" | "complete" | "pending" | "evidence_missing" | "delivery_failed";

/** `null` = the visitor was erased (the audit row remains, anonymised). */
export interface VisitorRef {
  id: string;
  number: number | null;
  label: string;
}

export interface VisitEventRecord {
  id: string;
  visit_id: string;
  type: EventType;
  visitor: VisitorRef | null;
  session_id: string;
  source_id: string;
  source_label: string;
  /** Wall-clock time of the first (entry) / last defensible (exit) observation. */
  event_time: string;
  video_ms: number;
  /** When the event was confirmed and stored. */
  recorded_at: string;
  image_status: ImageStatus;
  has_image: boolean;
  image_kind: string | null;
  persistence_status: PersistenceStatus;
  exit_basis: ExitBasis | null;
  closure_reason: string | null;
}

export interface EventList {
  items: VisitEventRecord[];
  total: number;
  limit: number;
  offset: number;
}

export interface VisitRecord {
  id: string;
  visitor: VisitorRef | null;
  session_id: string;
  source_id: string;
  source_label: string;
  state: VisitState;
  entry_time: string;
  last_observed_time: string;
  exit_time: string | null;
  exit_confirmed_at: string | null;
  exit_basis: ExitBasis | null;
  closure_reason: string | null;
  track_segments: number;
  /** Observed presence in video time; null while open or for an interrupted visit. */
  duration_seconds: number | null;
  integrity: Integrity;
}

export interface VisitDetailRecord extends VisitRecord {
  events: VisitEventRecord[];
}

export interface EventsHealth {
  enabled: boolean;
  events_by_persistence: Record<string, number>;
  events_by_image_status: Record<string, number>;
  open_visits: number;
  pending_image_retries: number;
  counters: Record<string, number>;
  events_log_present: boolean;
}

/** Live (WebSocket) messages. `event` is exactly what the events API returns minus storage details. */
export interface VisitEventMessage {
  type: "visit_event";
  event: Pick<
    VisitEventRecord,
    | "id"
    | "visit_id"
    | "type"
    | "visitor"
    | "event_time"
    | "session_id"
    | "source_label"
    | "image_status"
    | "persistence_status"
    | "exit_basis"
    | "closure_reason"
  >;
}

export interface VisitStateMessage {
  type: "visit_state";
  visit_id: string;
  visitor_id: string;
  visitor_number: number;
  state: VisitState;
}

export interface VisitsSnapshotMessage {
  type: "visits";
  visits: { visit_id: string; visitor_id: string; visitor_number: number; state: VisitState }[];
}

export type EventSort = "event_time" | "recorded_at";
