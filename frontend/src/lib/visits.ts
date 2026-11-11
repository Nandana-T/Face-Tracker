/**
 * Visit / event presentation helpers (Phase 4).
 *
 * Terminology is deliberate. An ENTRY is the first confirmed sighting of a visit. An EXIT is the
 * last defensible observation of the visit - never a measured departure: "left view" means the
 * visitor was not seen again for the configured period, "observation ended" means the video or the
 * monitoring stopped while the visitor was still being seen, "interrupted" means the pipeline
 * itself failed. Only a visit that closed normally has a duration.
 */
import type {
  EventType,
  ExitBasis,
  ImageStatus,
  Integrity,
  PersistenceStatus,
  VisitEventMessage,
  VisitEventRecord,
  VisitRecord,
  VisitState,
} from "./api-types";

export const EVENT_TYPE_LABEL: Record<EventType, string> = {
  entry: "Entry",
  exit: "Exit",
  interrupted: "Interrupted",
};

export const VISIT_STATE_LABEL: Record<VisitState, string> = {
  ACTIVE: "In view",
  TEMPORARILY_MISSING: "Not in view (waiting)",
  EXIT_PENDING: "Closing",
  CLOSED: "Closed",
  INTERRUPTED: "Interrupted",
};

export const EXIT_BASIS_LABEL: Record<ExitBasis, string> = {
  absence: "Left the view",
  observation_ended: "Observation ended",
  interrupted: "Processing interrupted",
};

export const EXIT_BASIS_HELP: Record<ExitBasis, string> = {
  absence: "Not seen again for the configured period. The time is the last sighting, not a measured departure.",
  observation_ended:
    "The video ended or monitoring was stopped while the visitor was still being seen. This does not mean they left the location.",
  interrupted: "The worker, source or API stopped unexpectedly. The end of this visit is unknown.",
};

export const CLOSURE_REASON_LABEL: Record<string, string> = {
  absence_timeout: "Not seen for the grace period",
  end_of_stream: "End of the video",
  manual_stop: "Monitoring stopped by a user",
  worker_lost: "Video worker lost",
  api_shutdown: "API shut down",
  api_restarted: "API restarted",
  source_error: "Video source failed",
};

export const IMAGE_STATUS_LABEL: Record<ImageStatus, string> = {
  STORED: "Stored",
  PENDING: "Being stored",
  UNAVAILABLE: "No usable image captured",
  MISSING: "Image file missing",
  ERASED: "Erased",
};

export const PERSISTENCE_LABEL: Record<PersistenceStatus, string> = {
  DELIVERED: "Recorded",
  PENDING: "Recording…",
  DEAD: "Log delivery failed",
};

export const INTEGRITY_LABEL: Record<Integrity, string> = {
  open: "Visit in progress",
  complete: "Complete",
  pending: "Recording in progress",
  evidence_missing: "Evidence image missing",
  delivery_failed: "Log delivery failed",
};

export const IMAGE_KIND_LABEL: Record<string, string> = {
  observation: "Best observation at entry",
  last_good_observation: "Last good observation (not a new capture)",
  entry_representative: "Entry image reused (no later image was available)",
};

/** "2 min 05 s" - only for a real number of seconds. */
export function formatDuration(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return null;
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
}

export function eventHeadline(e: Pick<VisitEventRecord, "type" | "exit_basis">): string {
  if (e.type === "entry") return "Entry";
  if (e.type === "interrupted") return "Interrupted";
  return e.exit_basis === "observation_ended" ? "Exit (observation ended)" : "Exit";
}

export function visitorText(v: { label: string } | null): string {
  return v ? v.label : "Erased visitor";
}

export interface VisitNotice {
  level: "success" | "info" | "warning";
  title: string;
  description: string;
}

/** Live notification for one durable event; the id deduplicates replays and reconnects. */
export function visitNotice(message: VisitEventMessage): VisitNotice {
  const e = message.event;
  const who = visitorText(e.visitor);
  switch (e.type) {
    case "entry":
      return { level: "success", title: `${who} entered`, description: `Entry recorded · ${e.source_label}.` };
    case "exit":
      return {
        level: "info",
        title: e.exit_basis === "observation_ended" ? `${who}: observation ended` : `${who} left the view`,
        description:
          e.exit_basis === "observation_ended"
            ? "The video ended or monitoring stopped; this is not a verified departure."
            : "Exit recorded at the last sighting.",
      };
    default:
      return {
        level: "warning",
        title: `${who}: visit interrupted`,
        description: "Processing stopped unexpectedly; the end of this visit is unknown.",
      };
  }
}

export function visitIsOpen(v: Pick<VisitRecord, "state">): boolean {
  return v.state === "ACTIVE" || v.state === "TEMPORARILY_MISSING" || v.state === "EXIT_PENDING";
}
