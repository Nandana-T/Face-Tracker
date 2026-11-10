import { ChevronDown } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type { SessionOut } from "@/lib/api-types";
import { formatConfidence } from "@/lib/geometry";
import type { LatestFrame } from "./use-monitoring";

interface Props {
  session: SessionOut | null;
  latest: LatestFrame | null;
  streamFps: number | null;
}

function Stat({ label, value, hint, testId }: { label: string; value: string; hint?: string; testId: string }) {
  return (
    <div className="rounded-md border p-3">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-1 font-mono text-xl font-semibold tabular-nums" data-testid={testId}>
        {value}
      </dd>
      {hint && <p className="mt-0.5 text-[11px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

const fmt = (n: number | undefined | null, digits = 0) =>
  n === undefined || n === null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: digits });

/** Real detector/ingestion counters only. Nothing here is a visitor count. */
export function DetectionSummary({ session, latest, streamFps }: Props) {
  const [open, setOpen] = useState(false);
  const has = session !== null && session.stats.frames_read > 0;
  const s = session?.stats;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Detection summary</CardTitle>
        <CardDescription>Faces found by the detector in this session.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <dl className="grid grid-cols-2 gap-3" aria-label="Detection statistics">
          <Stat
            label="Faces in latest frame"
            value={has ? fmt(latest?.faces ?? s?.faces_last_frame) : "—"}
            hint="Latest analysed frame"
            testId="stat-faces"
          />
          <Stat
            label="Processing FPS"
            value={has ? fmt(s?.processing_fps, 1) : "—"}
            hint="Frames analysed per second"
            testId="stat-fps"
          />
          <Stat label="Frames read" value={has ? fmt(s?.frames_read) : "—"} testId="stat-read" />
          <Stat
            label="Frames analysed"
            value={has ? fmt(s?.frames_processed) : "—"}
            hint="Ran the detector"
            testId="stat-processed"
          />
          <Stat
            label="Frames skipped"
            value={has ? fmt(s?.frames_skipped) : "—"}
            hint="Detector not run"
            testId="stat-skipped"
          />
          <Stat
            label="Inference time"
            value={has ? `${fmt(s?.avg_inference_ms, 1)} ms` : "—"}
            hint={has ? `last ${fmt(s?.last_inference_ms, 1)} ms` : "average per frame"}
            testId="stat-inference"
          />
        </dl>
        <p className="text-xs text-muted-foreground" data-testid="confidence-line">
          {latest && latest.bestConfidence !== null
            ? `Highest detection confidence in latest frame: ${formatConfidence(latest.bestConfidence)}`
            : has
              ? "No face detected in the latest frame."
              : "Confidence appears once frames are analysed."}
        </p>

        <div>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls="detection-details"
            className="flex w-full items-center justify-between rounded-md px-1 py-1 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            Detection details
            <ChevronDown className={`size-4 transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
          </button>
          <AnimatePresence initial={false}>
            {open && (
              <motion.div
                id="detection-details"
                key="details"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: "auto", opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <dl className="mt-2 space-y-1.5 text-sm">
                  <Detail label="Frame size" value={s && s.frame_width ? `${s.frame_width}×${s.frame_height}` : "—"} />
                  <Detail label="Source frame rate" value={s && s.source_fps ? `${fmt(s.source_fps, 1)} fps` : "—"} />
                  <Detail label="Streamed to this page" value={streamFps !== null ? `${fmt(streamFps, 1)} fps` : "—"} />
                  <Detail label="Model" value={session?.settings?.model ?? "—"} />
                  <Detail
                    label="Detector runs on"
                    value={session?.settings ? everyNth(session.settings.frame_skip + 1) : "—"}
                  />
                  <Detail
                    label="Confidence threshold"
                    value={session?.settings ? formatConfidence(session.settings.confidence_threshold) : "—"}
                  />
                  <Detail label="Frames dropped (back-pressure)" value={has ? fmt(s?.frames_dropped) : "—"} />
                  <Detail label="Unreadable frames" value={has ? fmt(s?.corrupt_frames) : "—"} />
                  <Detail label="Most faces in one frame" value={has ? fmt(s?.max_faces_in_frame) : "—"} />
                  <Detail label="Face detections (sum)" value={has ? fmt(s?.detections_total) : "—"} />
                </dl>
                <p className="mt-2 text-xs text-muted-foreground">
                  &ldquo;Face detections&rdquo; adds up the faces found in every analysed frame. The
                  same person appears in many frames, so this is not a count of visitors. Unique
                  visitor counting arrives in a later phase.
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </CardContent>
    </Card>
  );
}

/** `frame_skip = N` skips N frames between detections, so the detector runs on every (N+1)th. */
export function everyNth(n: number): string {
  if (n <= 1) return "every frame";
  const tens = n % 100;
  const suffix = tens >= 11 && tens <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[n % 10] ?? "th";
  return `every ${n}${suffix} frame`;
}

function Detail({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 border-b pb-1">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}
