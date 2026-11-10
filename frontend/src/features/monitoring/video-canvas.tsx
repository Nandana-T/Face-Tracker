import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef } from "react";
import type { FrameHeader } from "@/lib/api-types";
import { containRect, formatConfidence, mapBox } from "@/lib/geometry";
import { cn } from "@/lib/utils";

export interface VideoCanvasHandle {
  /** Takes ownership of `image` (closed when replaced). Never triggers a React render. */
  draw(image: ImageBitmap, header: FrameHeader): void;
  clear(): void;
}

interface VideoCanvasProps {
  label: string;
  className?: string;
  /** Visually dim the last frame (e.g. after the session ended). */
  dimmed?: boolean;
}

const BOX = "#22d3ee";
const LABEL_BG = "rgba(8, 47, 73, 0.85)";

/**
 * Draws the latest frame letterboxed ("contain") and the face boxes on top, on one canvas.
 *
 * Coordinates arrive in original-frame pixels and are mapped through the rectangle the frame
 * actually occupies, so boxes stay aligned when the container resizes or is letterboxed.
 * Drawing is coalesced with requestAnimationFrame and bypasses React state entirely.
 */
export const VideoCanvas = forwardRef<VideoCanvasHandle, VideoCanvasProps>(function VideoCanvas(
  { label, className, dimmed },
  ref,
) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const current = useRef<{ image: ImageBitmap; header: FrameHeader } | null>(null);
  const rafId = useRef<number | null>(null);
  const scheduled = useRef(false);

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
    const dpr = width / (canvas.clientWidth || width);
    ctx.lineWidth = Math.max(2, 2 * dpr);
    ctx.font = `${Math.round(12 * dpr)}px ui-monospace, monospace`;
    ctx.textBaseline = "middle";
    for (const face of header.faces) {
      const box = mapBox(face.bbox, header.frame_width, header.frame_height, drawn);
      ctx.strokeStyle = BOX;
      ctx.strokeRect(box.x, box.y, box.w, box.h);
      const text = formatConfidence(face.confidence);
      const padX = 5 * dpr;
      const tagH = 18 * dpr;
      const tagW = ctx.measureText(text).width + padX * 2;
      const tx = Math.min(Math.max(box.x, drawn.x), drawn.x + drawn.w - tagW);
      const ty = box.y - tagH >= drawn.y ? box.y - tagH : box.y;
      ctx.fillStyle = LABEL_BG;
      ctx.fillRect(tx, ty, tagW, tagH);
      ctx.fillStyle = "#e0f2fe";
      ctx.fillText(text, tx + padX, ty + tagH / 2);
    }
  }, []);

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

  return (
    <div ref={wrapRef} className={cn("relative size-full overflow-hidden bg-black", className)}>
      <canvas
        ref={canvasRef}
        role="img"
        aria-label={label}
        data-testid="video-canvas"
        className={cn("absolute inset-0 size-full transition-opacity duration-300", dimmed && "opacity-60")}
      />
    </div>
  );
});
