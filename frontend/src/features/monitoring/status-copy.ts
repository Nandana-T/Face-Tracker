import type { StatusTone } from "@/components/common/status-indicator";
import type { SessionOut, SourcesResponse } from "@/lib/api-types";

export type ViewKey =
  | "api_unreachable"
  | "no_source"
  | "worker_offline"
  | "model_unavailable"
  | "idle"
  | "connecting"
  | "processing"
  | "stopping"
  | "completed"
  | "stopped"
  | "camera_disconnected"
  | "source_problem"
  | "error";

export interface ViewState {
  key: ViewKey;
  tone: StatusTone;
  /** Short badge text. */
  label: string;
  title: string;
  /** Actionable explanation. */
  body: string;
}

interface Input {
  session: SessionOut | null;
  sources: SourcesResponse | undefined;
  sourcesFailed: boolean;
  /** Error code returned by a rejected start request, if any. */
  startErrorCode: string | null;
}

const SOURCE_CODES = new Set([
  "source_not_found",
  "source_invalid",
  "source_corrupted",
  "source_unavailable",
  "source_open_failed",
]);

/** Single place that decides what the monitoring page tells the user. Pure and exhaustively tested. */
export function describeView({ session, sources, sourcesFailed, startErrorCode }: Input): ViewState {
  if (session) {
    switch (session.state) {
      case "STARTING":
        return {
          key: "connecting",
          tone: "loading",
          label: "Connecting",
          title: "Connecting to the source…",
          body: "The worker is opening the source and loading the face detector. This usually takes a few seconds.",
        };
      case "RUNNING":
        if (session.connection === "reconnecting") {
          return {
            key: "camera_disconnected",
            tone: "warn",
            label: "Reconnecting",
            title: "Camera disconnected - reconnecting",
            body: "The stream was interrupted. The worker keeps retrying automatically; you can stop the session at any time.",
          };
        }
        return {
          key: "processing",
          tone: "ok",
          label: "Processing",
          title: "Processing",
          body: "Faces are being detected in the live frames. Detection does not identify people.",
        };
      case "STOPPING":
        return {
          key: "stopping",
          tone: "loading",
          label: "Stopping",
          title: "Stopping…",
          body: "Waiting for the worker to release the source.",
        };
      case "COMPLETED":
        return {
          key: "completed",
          tone: "ok",
          label: "Completed",
          title: "Processing completed",
          body: "The end of the video was reached. Start again to replay it.",
        };
      case "STOPPED":
        return {
          key: "stopped",
          tone: "idle",
          label: "Stopped",
          title: "Processing stopped",
          body: "The session was stopped. Start it again whenever you like.",
        };
      case "ERROR":
        return describeError(session.error?.code ?? "unknown", session.error?.message);
      default:
        break;
    }
  }
  if (startErrorCode) return describeError(startErrorCode);
  if (sourcesFailed || !sources) {
    return {
      key: "api_unreachable",
      tone: "error",
      label: "Backend offline",
      title: "Backend not reachable",
      body: 'The dashboard cannot reach the API. Start it with "scripts\\start-backend.ps1", then reload.',
    };
  }
  if (!sources.sources.some((s) => s.availability === "available")) {
    return {
      key: "no_source",
      tone: "warn",
      label: "No source",
      title: "No video source configured",
      body: "Add a sample video to datasets/samples or define a source in config/config.json, then reload.",
    };
  }
  if (!sources.worker.connected) return describeError("worker_unavailable");
  if (sources.worker.model_available === false) return describeError("model_unavailable");
  return {
    key: "idle",
    tone: "idle",
    label: "Ready",
    title: "Ready to start",
    body: "Choose a source and press Start processing.",
  };
}

function describeError(code: string, message?: string): ViewState {
  if (code === "model_unavailable") {
    return {
      key: "model_unavailable",
      tone: "error",
      label: "Model unavailable",
      title: "Face detection model unavailable",
      body: "The detector model is missing or corrupt on the worker machine. Run scripts\\download-model.ps1 there, restart the worker, and try again.",
    };
  }
  if (code === "camera_unavailable") {
    return {
      key: "camera_disconnected",
      tone: "error",
      label: "Camera disconnected",
      title: "Camera disconnected",
      body: "The camera could not be reached. Check its power, network and address, then retry.",
    };
  }
  if (code === "worker_unavailable" || code === "worker_lost" || code === "worker_lost_session") {
    return {
      key: "worker_offline",
      tone: "error",
      label: "Worker offline",
      title: "Video worker not connected",
      body: 'Processing runs in a separate worker process. Start it with "scripts\\start-worker.ps1" and try again.',
    };
  }
  if (SOURCE_CODES.has(code)) {
    return {
      key: "source_problem",
      tone: "error",
      label: "Source problem",
      title: "The video source cannot be used",
      body: message ?? "The file is missing, unreadable or not a valid video. Pick another source.",
    };
  }
  if (code === "session_active") {
    return {
      key: "error",
      tone: "warn",
      label: "Busy",
      title: "Another session is active",
      body: "Stop the running session before starting a different source.",
    };
  }
  return {
    key: "error",
    tone: "error",
    label: "Error",
    title: "Processing error",
    body: message ?? "Something went wrong while processing. Check the worker logs and try again.",
  };
}

export const ACTIVE_VIEW_KEYS: readonly ViewKey[] = ["connecting", "processing", "stopping", "camera_disconnected"];

export function sourceTypeLabel(type: "file" | "rtsp" | "webcam"): string {
  if (type === "webcam") return "Live webcam";
  return type === "file" ? "Video file" : "RTSP camera";
}
