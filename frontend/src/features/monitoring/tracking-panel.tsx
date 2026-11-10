import { ChevronDown, Eye, EyeOff, Tag, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SessionOut, Track } from "@/lib/api-types";
import { formatConfidence } from "@/lib/geometry";
import { STATE_LABEL, formatDuration } from "@/lib/tracks";
import { cn } from "@/lib/utils";
import type { SelectedTrack } from "./use-monitoring";

interface Props {
  session: SessionOut | null;
  tracks: Track[];
  trackingOn: boolean;
  showOverlay: boolean;
  onShowOverlay: (v: boolean) => void;
  showLabels: boolean;
  onShowLabels: (v: boolean) => void;
  selected: SelectedTrack | null;
  onSelect: (track: Track | null) => void;
}

const fmt = (n: number | undefined | null) =>
  n === undefined || n === null ? "—" : n.toLocaleString();

const STATE_TONE: Record<Track["state"], string> = {
  CONFIRMED: "bg-green-500/15 text-green-700 dark:text-green-300",
  TENTATIVE: "bg-yellow-500/15 text-yellow-800 dark:text-yellow-300",
  LOST: "bg-zinc-500/15 text-zinc-700 dark:text-zinc-300",
};

function Count({ label, value, testId, hint }: { label: string; value: string; testId: string; hint: string }) {
  return (
    <div className="rounded-md border p-2.5">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 font-mono text-lg font-semibold tabular-nums" data-testid={testId}>
        {value}
      </dd>
      <p className="text-[11px] text-muted-foreground">{hint}</p>
    </div>
  );
}

/**
 * Tracking diagnostics. Every number comes from the backend's session stats or the latest frame.
 * Track ids are TEMPORARY, scoped to this processing session; the totals here are numbers of
 * tracks, never a count of people or visitors.
 */
export function TrackingPanel({
  session,
  tracks,
  trackingOn,
  showOverlay,
  onShowOverlay,
  showLabels,
  onShowLabels,
  selected,
  onSelect,
}: Props) {
  const [diagOpen, setDiagOpen] = useState(false);
  const s = session?.stats;
  const enabled = session?.settings?.tracking_enabled ?? trackingOn;
  const has = !!s && s.frames_processed > 0 && !!s.tracks_enabled;

  return (
    <Card data-testid="tracking-panel">
      <CardHeader>
        <CardTitle>Tracking</CardTitle>
        <CardDescription>
          Temporary track ids within this session. They are not visitor identities.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {session && !enabled ? (
          <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground" data-testid="tracking-disabled">
            Tracking is switched off in the server configuration (tracking.enabled). Only raw
            detections are shown.
          </p>
        ) : null}

        <div className="flex flex-wrap gap-2" role="group" aria-label="Overlay options">
          <Button
            type="button"
            variant={showOverlay ? "default" : "outline"}
            size="sm"
            aria-pressed={showOverlay}
            onClick={() => onShowOverlay(!showOverlay)}
            data-testid="toggle-overlay"
          >
            {showOverlay ? <Eye className="size-4" aria-hidden /> : <EyeOff className="size-4" aria-hidden />}
            Boxes
          </Button>
          <Button
            type="button"
            variant={showLabels ? "default" : "outline"}
            size="sm"
            aria-pressed={showLabels}
            disabled={!showOverlay}
            onClick={() => onShowLabels(!showLabels)}
            data-testid="toggle-labels"
          >
            <Tag className="size-4" aria-hidden />
            Labels
          </Button>
        </div>

        <dl className="grid grid-cols-3 gap-2" aria-label="Track statistics">
          <Count label="Confirmed" value={has ? fmt(s?.tracks_confirmed) : "—"} hint="now" testId="trk-confirmed" />
          <Count label="Tentative" value={has ? fmt(s?.tracks_tentative) : "—"} hint="not yet confirmed" testId="trk-tentative" />
          <Count label="Lost" value={has ? fmt(s?.tracks_lost) : "—"} hint="temporarily unseen" testId="trk-lost" />
        </dl>

        <div>
          <h3 className="mb-1.5 text-xs font-medium text-muted-foreground">Tracks in the latest frame</h3>
          {tracks.length === 0 ? (
            <p className="text-xs text-muted-foreground" data-testid="track-list-empty">
              {trackingOn || has ? "No active tracks right now." : "Tracks appear once frames are analysed."}
            </p>
          ) : (
            <ul className="flex flex-wrap gap-1.5" aria-label="Active tracks" data-testid="track-list">
              {tracks.map((t) => {
                const isSel = selected?.track.track_id === t.track_id;
                return (
                  <li key={t.track_id}>
                    <button
                      type="button"
                      onClick={() => onSelect(isSel ? null : t)}
                      aria-pressed={isSel}
                      data-testid={`track-chip-${t.track_id}`}
                      className={cn(
                        "inline-flex items-center gap-1.5 rounded-md border px-2 py-1 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                        isSel && "border-primary ring-1 ring-primary",
                      )}
                    >
                      #{t.track_id}
                      <span className={cn("rounded px-1 text-[10px] font-sans", STATE_TONE[t.state])}>
                        {STATE_LABEL[t.state]}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        <AnimatePresence initial={false} mode="wait">
          {selected ? (
            <motion.section
              key="details"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden rounded-md border border-primary/60"
              aria-label={`Track ${selected.track.track_id} details`}
              data-testid="track-details"
            >
              <TrackDetails selected={selected} onClose={() => onSelect(null)} />
            </motion.section>
          ) : null}
        </AnimatePresence>

        <div>
          <button
            type="button"
            onClick={() => setDiagOpen((v) => !v)}
            aria-expanded={diagOpen}
            aria-controls="tracking-diagnostics"
            className="flex w-full items-center justify-between rounded-md px-1 py-1 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
            data-testid="diagnostics-toggle"
          >
            Tracking diagnostics
            <ChevronDown className={`size-4 transition-transform ${diagOpen ? "rotate-180" : ""}`} aria-hidden />
          </button>
          <AnimatePresence initial={false}>
            {diagOpen && (
              <motion.div
                id="tracking-diagnostics"
                key="diag"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <dl className="mt-2 space-y-1.5 text-sm" data-testid="diagnostics">
                  <Row label="Tracks created this session" value={has ? fmt(s?.tracks_created_total) : "—"} testId="trk-created" />
                  <Row label="…reached CONFIRMED" value={has ? fmt(s?.tracks_confirmed_total) : "—"} testId="trk-confirmed-total" />
                  <Row label="…rejected before confirmation" value={has ? fmt(s?.tracks_rejected_total) : "—"} testId="trk-rejected" />
                  <Row label="Confirmed tracks removed (expired)" value={has ? fmt(s?.tracks_removed_total) : "—"} testId="trk-removed" />
                  <Row label="Recoveries (lost → confirmed)" value={has ? fmt(s?.tracks_recovered_total) : "—"} testId="trk-recovered" />
                  <Row
                    label="Tracking time per frame"
                    value={has ? `${(s?.tracking_ms_avg ?? 0).toFixed(2)} ms` : "—"}
                    testId="trk-ms"
                  />
                  <Row label="Tracker errors" value={has ? fmt(s?.tracks_errors) : "—"} testId="trk-errors" />
                  <Row label="ID switches / fragmentation" value="not measurable without ground truth" testId="trk-idsw" />
                </dl>
                <p className="mt-2 text-xs text-muted-foreground">
                  &ldquo;Tracks created&rdquo; counts every temporary track, including short-lived
                  ones that were rejected and a person who leaves and returns (who gets a new
                  track). It is not a count of people or visitors.
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </CardContent>
    </Card>
  );
}

function Row({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="flex justify-between gap-4 border-b pb-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-medium tabular-nums" data-testid={testId}>
        {value}
      </dd>
    </div>
  );
}

function TrackDetails({ selected, onClose }: { selected: SelectedTrack; onClose: () => void }) {
  const t = selected.track;
  const [x1, y1, x2, y2] = t.bbox;
  return (
    <div className="space-y-2 p-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-mono text-sm font-semibold">Track #{t.track_id}</h3>
        <div className="flex items-center gap-2">
          <span
            className={cn("rounded px-1.5 py-0.5 text-xs", selected.ended ? "bg-muted text-muted-foreground" : STATE_TONE[t.state])}
            data-testid="detail-state"
          >
            {selected.ended ? "Ended" : STATE_LABEL[t.state]}
          </span>
          <Button type="button" variant="ghost" size="icon" aria-label="Close track details" onClick={onClose} data-testid="detail-close">
            <X className="size-4" aria-hidden />
          </Button>
        </div>
      </div>
      {selected.ended ? (
        <p className="text-xs text-muted-foreground" data-testid="detail-ended">
          This track is no longer active. Showing its last known values.
        </p>
      ) : null}
      <dl className="space-y-1">
        <Row label="First seen" value={formatDuration(t.first_seen_ms)} testId="detail-first" />
        <Row label="Last seen (detected)" value={formatDuration(t.last_seen_ms)} testId="detail-last" />
        <Row label="Age" value={formatDuration(t.age_ms)} testId="detail-age" />
        <Row
          label="Last observed confidence"
          value={t.last_observed_confidence === null ? "—" : formatConfidence(t.last_observed_confidence)}
          testId="detail-conf"
        />
        <Row
          label="Box now"
          value={`${x1},${y1} – ${x2},${y2} (${x2 - x1}×${y2 - y1})`}
          testId="detail-box"
        />
        <Row label="Box source" value={t.observed ? "detected in this frame" : "predicted (no detection)"} testId="detail-source" />
        <Row label="Missed updates in a row" value={String(t.missed_updates)} testId="detail-missed" />
        <Row label="Detections matched" value={String(t.hits)} testId="detail-hits" />
      </dl>
      <p className="text-[11px] text-muted-foreground">
        Times are video time. The id is temporary and only meaningful inside this session.
      </p>
    </div>
  );
}
