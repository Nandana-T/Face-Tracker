/** Pure helpers for drawing and describing tracks. Track ids are temporary and session-scoped. */
import type { Track, TrackState } from "./api-types";
import { formatConfidence, mapBox, type Rect } from "./geometry";

export interface TrackStyle {
  color: string;
  /** Dash pattern for predicted (not observed) boxes. */
  dash: number[];
  lineWidth: number;
}

const COLORS: Record<TrackState, string> = {
  CONFIRMED: "#22c55e",
  TENTATIVE: "#facc15",
  LOST: "#9ca3af",
};

/** Observed boxes are solid; predicted ones (tentative/lost without a fresh detection) are dashed. */
export function trackStyle(track: Pick<Track, "state" | "observed">, selected: boolean): TrackStyle {
  return {
    color: selected ? "#ffffff" : COLORS[track.state],
    dash: track.observed ? [] : [6, 4],
    lineWidth: selected ? 3 : track.state === "TENTATIVE" ? 1 : 2,
  };
}

export const STATE_LABEL: Record<TrackState, string> = {
  CONFIRMED: "Confirmed",
  TENTATIVE: "Tentative",
  LOST: "Lost",
};

/** More than this many tracks and labels shrink to the bare id so the picture stays readable. */
export const CROWDED_TRACKS = 8;

export function trackLabel(track: Track, total: number): string {
  const base = `#${track.track_id}`;
  if (!track.observed) return track.state === "LOST" ? `${base} lost` : `${base} pred.`;
  if (total > CROWDED_TRACKS || track.detection_confidence === null) return base;
  return `${base} ${formatConfidence(track.detection_confidence)}`;
}

export function formatDuration(ms: number): string {
  const s = Math.max(0, ms) / 1000;
  return s < 10 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`;
}

/**
 * The track under a point (canvas coordinates), preferring the smallest box when several overlap
 * so a small face inside a larger one stays selectable.
 */
export function hitTest(
  tracks: readonly Track[],
  x: number,
  y: number,
  frameW: number,
  frameH: number,
  drawn: Rect,
): Track | null {
  let best: { track: Track; area: number } | null = null;
  for (const track of tracks) {
    const b = mapBox(track.bbox, frameW, frameH, drawn);
    if (x >= b.x && x <= b.x + b.w && y >= b.y && y <= b.y + b.h) {
      const area = b.w * b.h;
      if (!best || area < best.area) best = { track, area };
    }
  }
  return best?.track ?? null;
}
