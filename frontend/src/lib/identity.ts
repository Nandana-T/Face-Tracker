/**
 * Recognition presentation helpers.
 *
 * Three identifiers are never interchangeable: a TRACK id (`#7`) is temporary and session-scoped,
 * a VISITOR (`V00124`) is a persistent facial identity, and a visit id does not exist yet.
 * Labels always show what is actually known - "Recognizing" means undecided, not "unknown person".
 */
import type { IdentityEventMessage, IdentityState, Track, TrackIdentity } from "./api-types";

export const IDENTITY_STATE_LABEL: Record<IdentityState, string> = {
  UNRESOLVED: "Recognizing",
  INSUFFICIENT_QUALITY: "Low quality",
  MATCH_PENDING: "Recognizing",
  RECOGNIZED: "Recognized",
  AMBIGUOUS: "Ambiguous",
  REGISTRATION_PENDING: "Registering",
  REGISTERED: "New visitor",
  REJECTED: "Unverified",
};

/** Box colours that override the track-state colour (recognised/registered/ambiguous faces stand out). */
export const IDENTITY_COLOR: Partial<Record<IdentityState, string>> = {
  RECOGNIZED: "#a78bfa",
  REGISTERED: "#2dd4bf",
  AMBIGUOUS: "#f59e0b",
};

export function identityColor(identity: TrackIdentity | undefined): string | null {
  return identity ? (IDENTITY_COLOR[identity.state] ?? null) : null;
}

/**
 * Canvas label for a CONFIRMED, currently detected track. `active` = recognition is running for
 * this session (otherwise the plain tracking label is used by the caller).
 */
export function identityLabel(track: Pick<Track, "track_id">, identity: TrackIdentity | undefined): string {
  const id = `#${track.track_id}`;
  if (identity?.visitor && (identity.state === "RECOGNIZED" || identity.state === "REGISTERED")) {
    return identity.state === "REGISTERED"
      ? `${identity.visitor.label} new · ${id}`
      : `${identity.visitor.label} · ${id}`;
  }
  return `${id} · ${identity ? IDENTITY_STATE_LABEL[identity.state] : "Recognizing"}`;
}

export interface Notice {
  level: "success" | "info" | "warning" | "error";
  title: string;
  description: string;
}

/** The notification (if any) for a server-reported transition. Silent state changes return null. */
export function noticeFor(e: IdentityEventMessage): Notice | null {
  const who = e.visitor?.label ?? "";
  switch (e.event) {
    case "registered":
      return {
        level: "success",
        title: `New visitor registered: ${who}`,
        description: `First reliable sighting on track #${e.track_id}.`,
      };
    case "recognized":
      return {
        level: "info",
        title: `Visitor recognized: ${who}`,
        description:
          e.reason === "reassigned_after_conflict"
            ? `Track #${e.track_id} was re-assigned after conflicting evidence.`
            : `Track #${e.track_id} matched an existing visitor.`,
      };
    case "ambiguous":
      return {
        level: "warning",
        title: "Ambiguous identity",
        description:
          e.reason === "same_visitor_on_two_tracks"
            ? `Track #${e.track_id} resembles a visitor already on another track; no identity assigned.`
            : `Track #${e.track_id} could not be told apart reliably; no identity assigned.`,
      };
    case "failed":
      return {
        level: "error",
        title: "Registration failed",
        description: `Track #${e.track_id} could not be registered; it will be retried with new evidence.`,
      };
    default:
      return null;
  }
}

/** One notification per (event, track, visitor) per session - a replay after reconnect is silent. */
export function noticeKey(e: IdentityEventMessage): string {
  return `${e.session_id}:${e.event}:${e.track_id}:${e.visitor?.id ?? ""}`;
}
