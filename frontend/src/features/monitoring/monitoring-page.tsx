import {
  AlertTriangle,
  CameraOff,
  CheckCircle2,
  Cpu,
  Loader2,
  PlugZap,
  ScanFace,
  ServerOff,
  Video,
  type LucideIcon,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useMemo, useRef, useState } from "react";
import { PageHeader } from "@/components/common/page-header";
import { StatusIndicator } from "@/components/common/status-indicator";
import { Card } from "@/components/ui/card";
import { ApiError } from "@/lib/api";
import { DetectionSummary } from "./detection-summary";
import { SourceControls } from "./source-controls";
import { RecognitionPanel } from "./recognition-panel";
import { VisitActivityPanel } from "./visit-activity-panel";
import { TrackingPanel } from "./tracking-panel";
import { describeView, sourceTypeLabel, type ViewKey } from "./status-copy";
import { useMonitoring } from "./use-monitoring";
import { VideoCanvas, type VideoCanvasHandle } from "./video-canvas";

const overlayIcon: Record<ViewKey, LucideIcon> = {
  api_unreachable: ServerOff,
  no_source: Video,
  worker_offline: PlugZap,
  model_unavailable: Cpu,
  idle: ScanFace,
  connecting: Loader2,
  processing: ScanFace,
  stopping: Loader2,
  completed: CheckCircle2,
  stopped: Video,
  camera_disconnected: CameraOff,
  source_problem: AlertTriangle,
  error: AlertTriangle,
};

const formatClock = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

export function MonitoringPage() {
  const canvasRef = useRef<VideoCanvasHandle>(null);
  const m = useMonitoring(canvasRef);
  const [choice, setChoice] = useState<string | null>(null);

  const sourceList = useMemo(() => m.sources.data?.sources ?? [], [m.sources.data]);

  // Derived during render (no effect): the active session's source is locked in; otherwise the
  // user's choice if it is still available, else the first available source.
  const firstAvailable = sourceList.find((s) => s.availability === "available")?.id ?? null;
  const choiceStillValid =
    choice !== null && sourceList.some((s) => s.id === choice && s.availability === "available");
  const selected =
    m.active && m.session?.source ? m.session.source.id : choiceStillValid ? choice : firstAvailable;

  const startErrorCode = m.startError instanceof ApiError ? (m.startError.code ?? "error") : null;
  const view = describeView({
    session: m.session,
    sources: m.sources.data,
    sourcesFailed: m.sources.isError && !m.sources.data,
    startErrorCode: m.active ? null : startErrorCode,
  });

  const worker = m.sources.data?.worker;
  let startBlockedReason: string | null = null;
  if (!m.sources.data) startBlockedReason = "Waiting for the backend.";
  else if (!worker?.connected) startBlockedReason = "The video worker is not connected.";
  else if (worker.model_available === false) startBlockedReason = "The face detection model is unavailable.";
  else if (!selected) startBlockedReason = "Select an available source.";
  const canStart = startBlockedReason === null;

  // Why recognition is not running, from what the backend reported (never guessed).
  const recognitionUnavailable: string | null =
    worker?.connected && worker.recognition_available === false
      ? (worker.recognition_detail ?? "Face recognition is unavailable on the worker.")
      : m.session && m.session.settings && !m.session.settings.recognition_enabled
        ? "Recognition was not active for this session (tracking is required and the worker must have the recognition model)."
        : null;

  const sessionSource = m.session?.source ?? sourceList.find((s) => s.id === selected) ?? null;
  const hasFrame = m.latest !== null;
  const showOverlay = !hasFrame || ["connecting", "stopping"].includes(view.key);
  // With a frame on screen, outcomes (stopped, completed, reconnecting, errors) are explained in a
  // slim banner so the last picture - and any boxes on it - stay visible.
  const showBanner = hasFrame && !showOverlay && !["processing", "idle"].includes(view.key);
  const Icon = overlayIcon[view.key];
  const spinning = view.key === "connecting" || view.key === "stopping";
  const canvasLabel = m.latest
    ? `Live video preview, ${m.latest.faces} ${m.latest.faces === 1 ? "face" : "faces"} detected in the latest analysed frame` +
      (m.trackingOn ? `, ${m.tracks.length} ${m.tracks.length === 1 ? "track" : "tracks"} shown` : "")
    : "Video preview (no frames yet)";

  return (
    <>
      <PageHeader
        title="Live Monitoring"
        description="Watch face detection run on a video source in real time."
      />

      {/* Announced to assistive technology whenever the backend-confirmed state changes. */}
      <p className="sr-only" role="status" aria-live="polite" data-testid="status-announcement">
        {view.title}. {view.body}
      </p>

      <div className="grid gap-6 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          <Card className="overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium" data-testid="viewport-source">
                  {sessionSource ? sessionSource.label : "No source selected"}
                </p>
                <p className="text-xs text-muted-foreground">
                  {sessionSource ? sourceTypeLabel(sessionSource.type) : "—"}
                </p>
              </div>
              <StatusIndicator label="" value={view.label} tone={view.tone} />
            </div>

            <div className="relative aspect-video bg-black" data-testid="viewport" data-view={view.key}>
              <VideoCanvas
                ref={canvasRef}
                label={canvasLabel}
                dimmed={!!m.session && !m.active}
                onTrackClick={m.selectTrack}
              />
              {/* One message at a time: "wait" lets the old one finish leaving before the next enters,
                  so the same heading never appears twice (visually or for screen readers). */}
              <AnimatePresence mode="wait">
                {showOverlay ? (
                  <motion.div
                    key={`overlay-${view.key}`}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.15 }}
                    className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-black/70 px-6 text-center text-white"
                    data-testid="viewport-overlay"
                  >
                    <Icon className={`size-9 text-cyan-300 ${spinning ? "animate-spin" : ""}`} aria-hidden />
                    <h2 className="text-base font-semibold">{view.title}</h2>
                    <p className="max-w-md text-sm text-white/80">{view.body}</p>
                  </motion.div>
                ) : showBanner ? (
                  <motion.div
                    key={`banner-${view.key}`}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, y: 8 }}
                    transition={{ duration: 0.15 }}
                    className="absolute inset-x-0 bottom-0 flex items-start gap-3 bg-black/80 px-4 py-3 text-white"
                    data-testid="viewport-banner"
                  >
                    <Icon className="mt-0.5 size-5 shrink-0 text-cyan-300" aria-hidden />
                    <div className="min-w-0">
                      <h2 className="text-sm font-semibold">{view.title}</h2>
                      <p className="text-xs text-white/80">{view.body}</p>
                    </div>
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </div>

            <div
              className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t px-4 py-2 font-mono text-xs text-muted-foreground"
              data-testid="frame-info"
            >
              {m.latest ? (
                <>
                  <span>Frame #{m.latest.frameId}</span>
                  <span>{formatClock(m.latest.timestampMs)}</span>
                  <span>
                    {m.latest.width}×{m.latest.height}
                  </span>
                  <span>{m.streamFps !== null ? `${m.streamFps} fps streamed` : "— fps streamed"}</span>
                  <span>{m.latest.inferenceMs.toFixed(0)} ms inference</span>
                </>
              ) : (
                <span>No frames received yet</span>
              )}
            </div>
          </Card>

          <p className="rounded-md border border-dashed px-4 py-3 text-xs text-muted-foreground">
            Face detection, temporary tracking and recognition: boxes show where a face-like region
            was found, and a &ldquo;#id&rdquo; follows the same face from frame to frame within this session.
            A &ldquo;V…&rdquo; label is a persistent visitor identity assigned only after repeated,
            reliable observations; &ldquo;Recognizing&rdquo; means undecided, not unknown.
            Solid boxes were detected in the frame; dashed boxes are predicted positions. A track id
            is not a person&rsquo;s identity: it does not identify anyone, is not kept across
            sessions, and is not a unique-visitor count. Frames are shown live and are not stored.
          </p>
        </div>

        <div className="space-y-4">
          <SourceControls
            sources={sourceList}
            selectedId={selected}
            onSelect={setChoice}
            locked={m.active}
            active={m.active}
            canStart={canStart}
            startBlockedReason={startBlockedReason}
            showRetry={m.session?.state === "ERROR" || startErrorCode !== null}
            starting={m.start.isPending}
            stopping={m.stop.isPending || m.session?.state === "STOPPING"}
            onStart={() => selected && m.start.mutate(selected)}
            onStop={() => m.session && m.stop.mutate(m.session.id)}
          />
          <TrackingPanel
            session={m.session}
            tracks={m.tracks}
            identities={m.identities}
            trackingOn={m.trackingOn}
            showOverlay={m.showOverlay}
            onShowOverlay={m.setShowOverlay}
            showLabels={m.showLabels}
            onShowLabels={m.setShowLabels}
            selected={m.selected}
            onSelect={m.selectTrack}
          />
          <RecognitionPanel
            session={m.session}
            identities={m.identities}
            registeredThisSession={m.registeredThisSession}
            recognisedTracks={m.recognisedTracks}
            unavailableReason={recognitionUnavailable}
          />
          <VisitActivityPanel
            session={m.session}
            activeVisits={m.activeVisits}
            recentEvents={m.recentEvents}
          />
          <DetectionSummary session={m.session} latest={m.latest} streamFps={m.streamFps} />
        </div>
      </div>
    </>
  );
}
