/** Pure geometry for drawing detection boxes over a letterboxed video frame. */

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/**
 * The rectangle a `mediaW x mediaH` frame occupies when scaled to fit ("contain") inside a
 * `containerW x containerH` area, centred. Aspect ratio is preserved; the rest is letterbox bars.
 */
export function containRect(
  containerW: number,
  containerH: number,
  mediaW: number,
  mediaH: number,
): Rect {
  if (containerW <= 0 || containerH <= 0 || mediaW <= 0 || mediaH <= 0) {
    return { x: 0, y: 0, w: 0, h: 0 };
  }
  const scale = Math.min(containerW / mediaW, containerH / mediaH);
  const w = mediaW * scale;
  const h = mediaH * scale;
  return { x: (containerW - w) / 2, y: (containerH - h) / 2, w, h };
}

/**
 * Map a box given in ORIGINAL frame pixels (`[x1, y1, x2, y2]`, frame `frameW x frameH`) into
 * the coordinate space of the canvas, given the rectangle where the frame is drawn.
 */
export function mapBox(
  bbox: readonly [number, number, number, number],
  frameW: number,
  frameH: number,
  drawn: Rect,
): Rect {
  const sx = drawn.w / frameW;
  const sy = drawn.h / frameH;
  const [x1, y1, x2, y2] = bbox;
  return { x: drawn.x + x1 * sx, y: drawn.y + y1 * sy, w: (x2 - x1) * sx, h: (y2 - y1) * sy };
}

export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`;
}
