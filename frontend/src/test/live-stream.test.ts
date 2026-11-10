import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveStream, type LinkState, type StreamHandlers } from "@/lib/live-stream";
import { FakeSocket, emptyStats, frameHeader, packFrame, sessionFixture } from "./monitoring-mocks";

function setup(decode?: (j: Uint8Array) => Promise<ImageBitmap>) {
  FakeSocket.reset();
  const links: LinkState[] = [];
  const handlers = {
    onSession: vi.fn(),
    onStats: vi.fn(),
    onFrame: vi.fn(),
    onLink: vi.fn((s: LinkState) => links.push(s)),
  } satisfies StreamHandlers;
  const stream = new LiveStream(
    {
      url: "ws://test/stream",
      WebSocketImpl: FakeSocket as unknown as typeof WebSocket,
      backoffMs: [100, 200],
      decode: decode ?? (async () => ({ width: 1, height: 1, close: vi.fn() }) as unknown as ImageBitmap),
    },
    handlers,
  );
  return { stream, handlers, links };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("LiveStream", () => {
  it("connects with a binary-capable socket and reports link state", () => {
    const { stream, links } = setup();
    stream.connect();
    expect(FakeSocket.last.url).toBe("ws://test/stream");
    expect(FakeSocket.last.binaryType).toBe("arraybuffer");
    FakeSocket.last.open();
    expect(links).toEqual(["connecting", "open"]);
  });

  it("dispatches session, stats and decoded frames", async () => {
    const { stream, handlers } = setup();
    stream.connect();
    const ws = FakeSocket.last;
    ws.open();
    ws.message(JSON.stringify({ type: "snapshot", session: sessionFixture({ state: "RUNNING" }) }));
    ws.message(JSON.stringify({ type: "stats", stats: { ...emptyStats, frames_read: 7 } }));
    ws.message(packFrame(frameHeader({ frame_id: 12 })));
    await vi.advanceTimersByTimeAsync(0);
    expect(handlers.onSession).toHaveBeenCalledWith(expect.objectContaining({ state: "RUNNING" }));
    expect(handlers.onStats).toHaveBeenCalledWith(expect.objectContaining({ frames_read: 7 }));
    expect(handlers.onFrame).toHaveBeenCalledTimes(1);
    expect(handlers.onFrame.mock.calls[0]![1].frame_id).toBe(12);
  });

  it("keeps only the newest frame while a decode is in flight (no backlog)", async () => {
    let release: (() => void)[] = [];
    const decode = vi.fn(
      () =>
        new Promise<ImageBitmap>((resolve) => {
          release.push(() => resolve({ width: 1, height: 1, close: vi.fn() } as unknown as ImageBitmap));
        }),
    );
    const { stream, handlers } = setup(decode);
    stream.connect();
    const ws = FakeSocket.last;
    ws.open();
    for (let i = 0; i < 6; i++) ws.message(packFrame(frameHeader({ frame_id: i })));
    // Frame 0 is decoding; frames 1-4 were superseded by 5.
    release.forEach((r) => r());
    release = [];
    await vi.advanceTimersByTimeAsync(0);
    release.forEach((r) => r());
    await vi.advanceTimersByTimeAsync(0);
    const ids = handlers.onFrame.mock.calls.map((c) => c[1].frame_id);
    expect(ids).toEqual([0, 5]);
    expect(decode).toHaveBeenCalledTimes(2);
    expect(stream.droppedBeforeDecode.count).toBe(4);
  });

  it("ignores malformed frames and undecodable images without stopping", async () => {
    const decode = vi.fn().mockRejectedValueOnce(new Error("bad jpeg")).mockResolvedValue({ close: vi.fn() });
    const { stream, handlers } = setup(decode);
    stream.connect();
    const ws = FakeSocket.last;
    ws.open();
    ws.message(new Uint8Array([1, 2, 3]).buffer); // malformed
    ws.message("not json");
    ws.message(packFrame(frameHeader({ frame_id: 1 }))); // decode rejects
    await vi.advanceTimersByTimeAsync(0);
    ws.message(packFrame(frameHeader({ frame_id: 2 }))); // recovers
    await vi.advanceTimersByTimeAsync(0);
    expect(handlers.onFrame).toHaveBeenCalledTimes(1);
    expect(handlers.onFrame.mock.calls[0]![1].frame_id).toBe(2);
  });

  it("reconnects with back-off after an unexpected close", () => {
    const { stream, links } = setup();
    stream.connect();
    FakeSocket.last.open();
    FakeSocket.last.serverClose();
    expect(links.at(-1)).toBe("reconnecting");
    expect(FakeSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(100);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.last.serverClose(); // never opened -> next delay 200 ms
    vi.advanceTimersByTime(199);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.instances).toHaveLength(3);
  });

  it("does not reconnect once the server reported the session ended", () => {
    const { stream, links } = setup();
    stream.connect();
    const ws = FakeSocket.last;
    ws.open();
    ws.message(JSON.stringify({ type: "ended", session: sessionFixture({ state: "COMPLETED" }) }));
    ws.serverClose();
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.instances).toHaveLength(1);
    expect(links.at(-1)).toBe("closed");
  });

  it("close() releases the socket and cancels pending reconnects", () => {
    const { stream } = setup();
    stream.connect();
    FakeSocket.last.open();
    FakeSocket.last.serverClose(); // schedules a reconnect
    stream.close();
    vi.advanceTimersByTime(10_000);
    expect(FakeSocket.instances).toHaveLength(1);
    const { stream: s2 } = setup();
    s2.connect();
    const ws = FakeSocket.last;
    s2.close();
    expect(ws.closed).toBe(true);
  });

  it("closes bitmaps that finish decoding after close()", async () => {
    const bitmap = { close: vi.fn() };
    let resolve!: (b: ImageBitmap) => void;
    const decode = () => new Promise<ImageBitmap>((r) => (resolve = r));
    const { stream, handlers } = setup(decode);
    stream.connect();
    FakeSocket.last.open();
    FakeSocket.last.message(packFrame(frameHeader()));
    stream.close();
    resolve(bitmap as unknown as ImageBitmap);
    await vi.advanceTimersByTimeAsync(0);
    expect(bitmap.close).toHaveBeenCalled();
    expect(handlers.onFrame).not.toHaveBeenCalled();
  });
});
