import { Loader2, Upload, X } from "lucide-react";
import { useRef, useState, type ChangeEvent } from "react";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/toaster";
import { ApiError, api, type UploadHandle } from "@/lib/api";
import type { SourceOut } from "@/lib/api-types";

export const ACCEPTED_EXTENSIONS = [".mp4", ".avi", ".mkv", ".mov", ".webm"] as const;
/** Client-side sanity limit; the server enforces its own (configurable) limit and is authoritative. */
export const MAX_CLIENT_MB = 2048;

export function validateVideoFile(file: { name: string; size: number }): string | null {
  const dot = file.name.lastIndexOf(".");
  const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!(ACCEPTED_EXTENSIONS as readonly string[]).includes(ext)) {
    return `Unsupported file type. Choose a ${ACCEPTED_EXTENSIONS.join(", ")} video.`;
  }
  if (file.size === 0) return "That file is empty.";
  if (file.size > MAX_CLIENT_MB * 1024 * 1024) return `That file is larger than ${MAX_CLIENT_MB} MB.`;
  return null;
}

interface Props {
  disabled: boolean;
  onUploaded(source: SourceOut): void;
}

/**
 * Upload a video from this computer and process it. The file is sent to the API (authenticated),
 * validated there as a real video, and appears as a source. Faces in an uploaded video are processed
 * like any other source (visitors are registered), so the panel says so.
 */
export function UploadVideo({ disabled, onUploaded }: Props) {
  const input = useRef<HTMLInputElement>(null);
  const handle = useRef<UploadHandle | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [name, setName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const busy = progress !== null;

  const choose = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // allow choosing the same file again
    if (!file) return;
    const problem = validateVideoFile(file);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    setName(file.name);
    setProgress(0);
    const upload = api.uploadVideo(file, (fraction) => setProgress(fraction));
    handle.current = upload;
    upload.promise
      .then((source) => {
        toast.success("Video uploaded", { description: `${source.label} is ready to process.` });
        onUploaded(source);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.code === "aborted") return;
        setError(err instanceof Error ? err.message : "The upload failed.");
      })
      .finally(() => {
        setProgress(null);
        setName(null);
        handle.current = null;
      });
  };

  return (
    <div className="space-y-2 rounded-md border border-dashed p-3" data-testid="upload-panel">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">Upload a video</span>
        <input
          ref={input}
          type="file"
          accept={`video/*,${ACCEPTED_EXTENSIONS.join(",")}`}
          className="sr-only"
          tabIndex={-1}
          aria-hidden
          onChange={choose}
          data-testid="upload-input"
        />
        {busy ? (
          <Button type="button" size="sm" variant="outline" onClick={() => handle.current?.abort()} data-testid="upload-cancel">
            <X /> Cancel
          </Button>
        ) : (
          <Button type="button" size="sm" variant="outline" disabled={disabled} onClick={() => input.current?.click()} data-testid="upload-button">
            <Upload /> Choose file
          </Button>
        )}
      </div>
      {busy ? (
        <div role="status" aria-live="polite" className="space-y-1" data-testid="upload-progress">
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" aria-hidden />
            <span className="truncate">Uploading {name}…</span>
            <span className="ml-auto tabular-nums">{Math.round((progress ?? 0) * 100)}%</span>
          </div>
          <progress className="h-1.5 w-full" max={1} value={progress ?? 0} aria-label="Upload progress" />
        </div>
      ) : null}
      {error ? (
        <p role="alert" className="text-xs text-red-500" data-testid="upload-error">
          {error}
        </p>
      ) : null}
      <p className="text-[11px] text-muted-foreground">
        {ACCEPTED_EXTENSIONS.join(", ")}. Only upload videos you are allowed to process: faces in them are detected and
        visitors are registered. You can delete an uploaded video afterwards.
      </p>
    </div>
  );
}
