import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from "react";
import type { FrameHeader, Track, TrackIdentity } from "@/lib/api-types";
import { identityColor, identityLabel } from "@/lib/identity";
import { containRect, formatConfidence, mapBox } from "@/lib/geometry";
import { hitTest, trackLabel, trackStyle } from "@/lib/tracks";
import { cn } from "@/lib/utils";

export interface OverlayOptions {
  /** Draw track boxes at all (false = just the video). */
  show: boolean;
  /** Draw the "#id ..." tags. */
  labels: boolean;
  selectedId: number | null;
}

export interface VideoCanvasHandle {
  /** Takes ownership of `image` (closed when replaced). Never triggers a React render. */
  draw(image: ImageBitmap, header: FrameHeader): void;
  clear(): void;
  /** Changes what is drawn on top of the video and repaints the current frame. */
  setOverlay(options: OverlayOptions): void;
  /** Recognition results per track id (session-scoped). `null` = recognition not running. */
  setIdentities(identities: ReadonlyMap<number, TrackIdentity> | null): void;
}

interface VideoCanvasProps {
  label: string;
  className?: string;
  /** Visually dim the last frame (e.g. after the session ended). */
  dimmed?: boolean;
  /** Called when a track box is clicked (null = empty area). Keyboard users use the track list. */
  onTrackClick?: (track: Track | null) => void;
}

const BOX = "#22d3ee";
const LABEL_BG = "rgba(8, 47, 73, 0.85)";
const DEFAULT_OVERLAY: OverlayOptions = { show: true, labels: true, selectedId: null };

/**
 * Draws the latest frame letterboxed ("contain") and the overlay on top, on one canvas.
 *
 * With tracking, the overlay is the session's temporary tracks (stable "#id" labels; solid = a
 * detection matched in this frame, dashed = predicted position). Without it, the raw detections.
 * Coordinates arrive in original-frame pixels and are mapped through the rectangle the frame
 * actually occupies, so boxes stay aligned when the container resizes or is letterboxed.
 * Drawing is coalesced with requestAnimationFrame and bypasses React state entirely.
 */
export const VideoCanvas = forwardRef<VideoCanvasHandle, VideoCanvasProps>(function VideoCanvas(
  { label, className, dimmed, onTrackClick },
  ref,
) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const current = useRef<{ image: ImageBitmap; header: FrameHeader } | null>(null);
  const overlay = useRef<OverlayOptions>(DEFAULT_OVERLAY);
  const identities = useRef<ReadonlyMap<number, TrackIdentity> | null>(null);
  const rafId = useRef<number | null>(null);
  const scheduled = useRef(false);

  const tag = useCallback(
    (
      ctx: CanvasRenderingContext2D,
      text: string,
      box: { x: number; y: number },
      drawn: { x: number; y: number; w: number; h: number },
      dpr: number,
      color: string,
    ) => {
      const padX = 5 * dpr;
      const tagH = 18 * dpr;
      const tagW = ctx.measureText(text).width + padX * 2;
      const tx = Math.min(Math.max(box.x, drawn.x), drawn.x + drawn.w - tagW);
      const ty = box.y - tagH >= drawn.y ? box.y - tagH : box.y;
      ctx.fillStyle = LABEL_BG;
      ctx.fillRect(tx, ty, tagW, tagH);
      ctx.fillStyle = color;
      ctx.fillText(text, tx + padX, ty + tagH / 2);
    },
    [],
  );

  const paint = useCallback(() => {
    scheduled.current = false;
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    const { width, height } = canvas;
    ctx.clearRect(0, 0, width, height);
    const frame = current.current;
    if (!frame) return;
    const { image, header } = frame;
    const drawn = containRect(width, height, header.frame_width, header.frame_height);
    ctx.drawImage(image, drawn.x, drawn.y, drawn.w, drawn.h);
    const opts = overlay.current;
    if (!opts.show) return;
    const dpr = width / (canvas.clientWidth || width);
    ctx.font = `${Math.round(12 * dpr)}px ui-monospace, monospace`;
    ctx.textBaseline = "middle";
    const tracks = header.tracks;
    if (tracks && header.tracking) {
      // Tracking is on: draw tracks. Selected one last so it sits on top.
      const ordered = [...tracks].sort(
        (a, b) => Number(a.track_id === opts.selectedId) - Number(b.track_id === opts.selectedId),
      );
      for (const t of ordered) {
        const identity = identities.current?.get(t.track_id);
        const style = trackStyle(t, t.track_id === opts.selectedId);
        // A recognised / newly registered / ambiguous face gets its own colour while it is detected.
        const idColor = t.state === "CONFIRMED" && t.observed ? identityColor(identity) : null;
        if (idColor && t.track_id !== opts.selectedId) style.color = idColor;
        const box = mapBox(t.bbox, header.frame_width, header.frame_height, drawn);
        ctx.setLineDash(style.dash.map((d) => d * dpr));
        ctx.lineWidth = Math.max(1, style.lineWidth * dpr);
        ctx.strokeStyle = style.color;
        ctx.strokeRect(box.x, box.y, box.w, box.h);
        ctx.setLineDash([]);
        // Neutral label text: the box colour carries the state, and text stays legible on any picture.
        if (opts.labels) {
          const recognising = identities.current !== null && t.state === "CONFIRMED" && t.observed;
          const text = recognising ? identityLabel(t, identity) : trackLabel(t, tracks.length);
          tag(ctx, text, box, drawn, dpr, "#f8fafc");
        }
      }
      return;
    }
    ctx.lineWidth = Math.max(2, 2 * dpr);
    for (const face of header.faces) {
      const box = mapBox(face.bbox, header.frame_width, header.frame_height, drawn);
      ctx.strokeStyle = BOX;
      ctx.strokeRect(box.x, box.y, box.w, box.h);
      tag(ctx, formatConfidence(face.confidence), box, drawn, dpr, "#e0f2fe");
    }
  }, [tag]);

  const schedule = useCallback(() => {
    if (scheduled.current) return; // coalesce: at most one paint per animation frame
    scheduled.current = true;
    rafId.current = requestAnimationFrame(paint);
  }, [paint]);

  useImperativeHandle(
    ref,
    () => ({
      draw(image, header) {
        current.current?.image.close?.();
        current.current = { image, header };
        schedule();
      },
      clear() {
        current.current?.image.close?.();
        current.current = null;
        schedule();
      },
      setOverlay(options) {
        overlay.current = options;
        schedule();
      },
      setIdentities(next) {
        identities.current = next;
        schedule();
      },
    }),
    [schedule],
  );

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.round(wrap.clientWidth * dpr));
      canvas.height = Math.max(1, Math.round(wrap.clientHeight * dpr));
      schedule();
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(wrap);
    return () => {
      observer.disconnect();
      if (rafId.current !== null) cancelAnimationFrame(rafId.current);
      // The cancelled frame will never run `paint`, so the flag must be reset here; otherwise
      // (React StrictMode mounts effects twice) no paint would ever be scheduled again.
      scheduled.current = false;
      current.current?.image.close?.();
      current.current = null;
    };
  }, [schedule]);

  const onClick = useCallback(
    (event: React.MouseEvent<HTMLCanvasElement>) => {
      const frame = current.current;
      const canvas = canvasRef.current;
      if (!onTrackClick || !frame || !canvas || !frame.header.tracks || !overlay.current.show) return;
      const rect = canvas.getBoundingClientRect();
      const sx = canvas.width / (rect.width || canvas.width);
      const sy = canvas.height / (rect.height || canvas.height);
      const x = (event.clientX - rect.left) * sx;
      const y = (event.clientY - rect.top) * sy;
      const { frame_width: fw, frame_height: fh } = frame.header;
      const drawn = containRect(canvas.width, canvas.height, fw, fh);
      onTrackClick(hitTest(frame.header.tracks, x, y, fw, fh, drawn));
    },
    [onTrackClick],
  );

  return (
    <div ref={wrapRef} className={cn("relative size-full overflow-hidden bg-black", className)}>
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={label}
        data-testid="video-canvas"
        onClick={onClick}
        className={cn(
          "absolute inset-0 size-full transition-opacity duration-300",
          dimmed && "opacity-60",
          onTrackClick && "cursor-pointer",
        )}
      />
    </div>
  );
});
