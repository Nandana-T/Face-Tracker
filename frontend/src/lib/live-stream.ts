import type { FrameHeader, SessionOut, SessionStats } from "./api-types";
import { parseControl, unpackFrame } from "./protocol";

export type LinkState = "connecting" | "open" | "reconnecting" | "closed";

export interface StreamHandlers {
  onSession(session: SessionOut): void;
  onStats(stats: SessionStats): void;
  /** Called outside React's render cycle; the consumer owns (and must close) the bitmap. */
  onFrame(image: ImageBitmap, header: FrameHeader): void;
  onLink(state: LinkState): void;
}

export interface StreamOptions {
  url: string;
  WebSocketImpl?: typeof WebSocket;
  decode?: (jpeg: Uint8Array) => Promise<ImageBitmap>;
  /** Reconnect delays in ms; the last value repeats. */
  backoffMs?: number[];
}

const defaultDecode = (jpeg: Uint8Array) =>
  createImageBitmap(new Blob([jpeg as BlobPart], { type: "image/jpeg" }));

/**
 * Browser end of the live stream.
 *
 * Frames are decoded latest-wins: if a frame arrives while the previous one is still decoding it
 * replaces any older waiting frame, so a slow tab never accumulates a backlog. Reconnects with
 * back-off until the server says the session ended (or `close()` is called).
 */
export class LiveStream {
  private ws: WebSocket | null = null;
  private closed = false;
  private ended = false;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private decoding = false;
  private pending: { header: FrameHeader; jpeg: Uint8Array } | null = null;
  readonly droppedBeforeDecode = { count: 0 };

  constructor(
    private readonly options: StreamOptions,
    private readonly handlers: StreamHandlers,
  ) {}

  connect(): void {
    if (this.closed) return;
    const Impl = this.options.WebSocketImpl ?? WebSocket;
    this.handlers.onLink(this.attempt === 0 ? "connecting" : "reconnecting");
    const ws = new Impl(this.options.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => {
      this.attempt = 0;
      this.handlers.onLink("open");
    };
    ws.onmessage = (event) => this.onMessage(event.data as unknown);
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      if (this.closed || this.ended) {
        this.handlers.onLink("closed");
        return;
      }
      const delays = this.options.backoffMs ?? [500, 1000, 2000, 4000];
      const delay = delays[Math.min(this.attempt, delays.length - 1)] ?? 1000;
      this.attempt += 1;
      this.handlers.onLink("reconnecting");
      this.timer = setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => ws.close();
  }

  close(): void {
    this.closed = true;
    if (this.timer) clearTimeout(this.timer);
    this.pending = null;
    this.ws?.close();
    this.ws = null;
  }

  private onMessage(data: unknown): void {
    if (typeof data === "string") {
      const message = parseControl(data);
      if (!message) return;
      if (message.type === "stats") {
        this.handlers.onStats(message.stats);
        return;
      }
      this.handlers.onSession(message.session);
      if (message.type === "ended") this.ended = true;
      return;
    }
    if (!(data instanceof ArrayBuffer)) return;
    try {
      const frame = unpackFrame(data);
      if (this.pending) this.droppedBeforeDecode.count += 1;
      this.pending = frame;
      void this.pump();
    } catch {
      /* malformed frame: ignore, the stream continues */
    }
  }

  private async pump(): Promise<void> {
    if (this.decoding) return;
    this.decoding = true;
    try {
      while (this.pending && !this.closed) {
        const { header, jpeg } = this.pending;
        this.pending = null;
        try {
          const image = await (this.options.decode ?? defaultDecode)(jpeg);
          if (this.closed) image.close();
          else this.handlers.onFrame(image, header);
        } catch {
          /* undecodable JPEG: skip it */
        }
      }
    } finally {
      this.decoding = false;
    }
  }
}
