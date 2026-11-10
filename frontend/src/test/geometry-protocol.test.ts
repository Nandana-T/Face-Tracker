import { describe, expect, it } from "vitest";
import { containRect, formatConfidence, mapBox } from "@/lib/geometry";
import { ProtocolError, parseControl, unpackFrame } from "@/lib/protocol";
import { frameHeader, packFrame } from "./monitoring-mocks";

describe("containRect (letterboxing)", () => {
  it("fills the container when aspect ratios match", () => {
    expect(containRect(800, 450, 640, 360)).toEqual({ x: 0, y: 0, w: 800, h: 450 });
  });

  it("adds side bars for a taller frame", () => {
    // 400x400 into 800x450: scale 1.125 -> 450x450, centred horizontally.
    expect(containRect(800, 450, 400, 400)).toEqual({ x: 175, y: 0, w: 450, h: 450 });
  });

  it("adds top/bottom bars for a wider frame", () => {
    // 1920x540 into 800x450: scale 0.41666 -> 800x225, centred vertically.
    const r = containRect(800, 450, 1920, 540);
    expect(r.x).toBe(0);
    expect(r.w).toBeCloseTo(800);
    expect(r.h).toBeCloseTo(225);
    expect(r.y).toBeCloseTo(112.5);
  });

  it.each([
    [0, 450, 640, 360],
    [800, 0, 640, 360],
    [800, 450, 0, 360],
    [800, 450, 640, 0],
  ])("returns an empty rect for degenerate sizes (%s,%s,%s,%s)", (cw, ch, mw, mh) => {
    expect(containRect(cw, ch, mw, mh)).toEqual({ x: 0, y: 0, w: 0, h: 0 });
  });
});

describe("mapBox (original-frame pixels -> canvas pixels)", () => {
  it("scales a box when the frame fills the canvas", () => {
    const drawn = containRect(800, 450, 640, 360);
    expect(mapBox([100, 50, 200, 150], 640, 360, drawn)).toEqual({ x: 125, y: 62.5, w: 125, h: 125 });
  });

  it("accounts for the letterbox offset", () => {
    const drawn = containRect(800, 450, 400, 400); // x offset 175
    const box = mapBox([100, 100, 200, 200], 400, 400, drawn);
    expect(box).toEqual({ x: 175 + 112.5, y: 112.5, w: 112.5, h: 112.5 });
  });

  it("keeps a box on the same facial feature when the container is resized", () => {
    // The same normalised point in the frame must land on the same normalised point of the drawn
    // image regardless of container size.
    const bbox = [160, 90, 320, 270] as const;
    for (const [cw, ch] of [
      [320, 180],
      [800, 450],
      [500, 500],
      [1200, 300],
    ] as const) {
      const drawn = containRect(cw, ch, 640, 360);
      const b = mapBox(bbox, 640, 360, drawn);
      expect((b.x - drawn.x) / drawn.w).toBeCloseTo(160 / 640);
      expect((b.y - drawn.y) / drawn.h).toBeCloseTo(90 / 360);
      expect(b.w / drawn.w).toBeCloseTo(160 / 640);
      expect(b.h / drawn.h).toBeCloseTo(180 / 360);
    }
  });

  it("formats confidence as a rounded percentage", () => {
    expect(formatConfidence(0.934)).toBe("93%");
    expect(formatConfidence(1)).toBe("100%");
    expect(formatConfidence(0.005)).toBe("1%");
  });
});

describe("wire protocol", () => {
  it("unpacks a frame message", () => {
    const header = frameHeader({ frame_id: 9 });
    const { header: got, jpeg } = unpackFrame(packFrame(header));
    expect(got).toEqual(header);
    expect([jpeg[0], jpeg[1]]).toEqual([0xff, 0xd8]);
  });

  it.each([
    ["too short", new Uint8Array([0, 0]).buffer],
    ["header longer than message", new Uint8Array([0, 0, 0, 99, 1, 2]).buffer],
    ["header too large", new Uint8Array([0, 0xff, 0, 0, 1]).buffer],
  ])("rejects %s", (_name, buffer) => {
    expect(() => unpackFrame(buffer)).toThrow(ProtocolError);
  });

  it("rejects a payload that is not a JPEG or a header without frame fields", () => {
    const head = new TextEncoder().encode(JSON.stringify({ frame_width: 1, frame_height: 1, faces: [] }));
    const bad = new Uint8Array(4 + head.length + 3);
    new DataView(bad.buffer).setUint32(0, head.length, false);
    bad.set(head, 4);
    bad.set([1, 2, 3], 4 + head.length);
    expect(() => unpackFrame(bad.buffer)).toThrow("not a JPEG");

    const noFields = new TextEncoder().encode("{}");
    const m = new Uint8Array(4 + noFields.length + 2);
    new DataView(m.buffer).setUint32(0, noFields.length, false);
    m.set(noFields, 4);
    m.set([0xff, 0xd8], 4 + noFields.length);
    expect(() => unpackFrame(m.buffer)).toThrow("missing fields");
  });

  it("parses control messages and ignores junk", () => {
    expect(parseControl('{"type":"stats","stats":{}}')?.type).toBe("stats");
    expect(parseControl("not json")).toBeNull();
    expect(parseControl('{"no":"type"}')).toBeNull();
  });
});
