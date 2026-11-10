import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VideoCanvas, type VideoCanvasHandle } from "@/features/monitoring/video-canvas";
import type { Track } from "@/lib/api-types";
import { ProtocolError, unpackFrame } from "@/lib/protocol";
import { CROWDED_TRACKS, formatDuration, hitTest, trackLabel, trackStyle } from "@/lib/tracks";
import {
  FakeSocket,
  createController,
  emptyStats,
  frameHeader,
  installFakeStreaming,
  packFrame,
  processingFetch,
  sessionFixture,
  trackFixture,
  trackedHeader,
  type ProcessingController,
} from "./monitoring-mocks";
import { renderApp } from "./render";
import { canvasCalls } from "./setup";

const SID = "a".repeat(32);
const bitmap = () => ({ width: 640, height: 360, close: vi.fn() }) as unknown as ImageBitmap;
const labels = () => canvasCalls.filter((c) => c.op === "fillText").map((c) => c.args[0]);
const ops = (op: string) => canvasCalls.filter((c) => c.op === op);

describe("track helpers", () => {
  it("draws predicted boxes dashed and observed boxes solid", () => {
    expect(trackStyle({ state: "CONFIRMED", observed: true }, false).dash).toEqual([]);
    expect(trackStyle({ state: "LOST", observed: false }, false).dash.length).toBeGreaterThan(0);
    expect(trackStyle({ state: "TENTATIVE", observed: false }, false).dash.length).toBeGreaterThan(0);
  });

  it("uses distinct colours per state and highlights the selection", () => {
    const colors = new Set(
      (["CONFIRMED", "TENTATIVE", "LOST"] as const).map((s) => trackStyle({ state: s, observed: true }, false).color),
    );
    expect(colors.size).toBe(3);
    const sel = trackStyle({ state: "CONFIRMED", observed: true }, true);
    expect(sel.color).toBe("#ffffff");
    expect(sel.lineWidth).toBeGreaterThan(trackStyle({ state: "CONFIRMED", observed: true }, false).lineWidth);
  });

  it("labels observed tracks with id + confidence and predicted ones without inventing a confidence", () => {
    expect(trackLabel(trackFixture({ track_id: 7 }), 1)).toBe("#7 93%");
    expect(trackLabel(trackFixture({ track_id: 7, state: "LOST", observed: false, detection_confidence: null }), 1)).toBe("#7 lost");
    expect(trackLabel(trackFixture({ track_id: 8, state: "TENTATIVE", observed: false, detection_confidence: null }), 1)).toBe("#8 pred.");
  });

  it("shortens labels in crowded scenes", () => {
    expect(trackLabel(trackFixture({ track_id: 3 }), CROWDED_TRACKS + 1)).toBe("#3");
  });

  it("formats durations", () => {
    expect(formatDuration(480)).toBe("0.5 s");
    expect(formatDuration(65_400)).toBe("65 s");
    expect(formatDuration(-5)).toBe("0.0 s");
  });

  it("picks the smallest box under the pointer", () => {
    const big = trackFixture({ track_id: 1, bbox: [0, 0, 400, 400] });
    const small = trackFixture({ track_id: 2, bbox: [100, 100, 150, 150] });
    const drawn = { x: 0, y: 0, w: 400, h: 400 };
    expect(hitTest([big, small], 120, 120, 400, 400, drawn)?.track_id).toBe(2);
    expect(hitTest([big, small], 300, 300, 400, 400, drawn)?.track_id).toBe(1);
    expect(hitTest([big, small], 500, 500, 400, 400, drawn)).toBeNull();
  });
});

describe("protocol", () => {
  it("rejects a header whose tracks field is not a list", () => {
    const bad = packFrame({ ...frameHeader(), tracks: "nope" as unknown as Track[] });
    expect(() => unpackFrame(bad)).toThrow(ProtocolError);
  });

  it("accepts Phase 1 headers (no tracks) and tracked headers", () => {
    expect(unpackFrame(packFrame(frameHeader())).header.tracks).toBeUndefined();
    expect(unpackFrame(packFrame(trackedHeader([trackFixture()]))).header.tracks).toHaveLength(1);
  });
});

let originalRaf: typeof requestAnimationFrame;
beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 800 });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 450 });
  originalRaf = window.requestAnimationFrame;
  window.requestAnimationFrame = (cb) => {
    cb(0);
    return 1;
  };
});
afterEach(() => {
  window.requestAnimationFrame = originalRaf;
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
});

describe("VideoCanvas track overlay", () => {
  function mount(onTrackClick?: (t: Track | null) => void) {
    const ref = createRef<VideoCanvasHandle>();
    const utils = render(<VideoCanvas ref={ref} label="preview" onTrackClick={onTrackClick} />);
    canvasCalls.length = 0;
    return { ref, ...utils };
  }

  it("draws track ids instead of raw detection confidences when tracking is on", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ track_id: 4 }), trackFixture({ track_id: 9, bbox: [300, 60, 380, 160], detection_confidence: 0.71 })]));
    expect(labels()).toEqual(["#4 93%", "#9 71%"]);
    expect(ops("strokeRect")).toHaveLength(2);
  });

  it("falls back to raw detections for Phase 1 style frames", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), frameHeader());
    expect(labels()).toEqual(["93%"]);
  });

  it("distinguishes predicted (dashed) from observed (solid) boxes", () => {
    const { ref } = mount();
    ref.current!.draw(
      bitmap(),
      trackedHeader([trackFixture({ track_id: 1 }), trackFixture({ track_id: 2, state: "LOST", observed: false, detection_confidence: null })]),
    );
    const dashes = ops("setLineDash").map((c) => (c.args[0] as number[]).length);
    expect(dashes).toContain(0);
    expect(dashes.some((n) => n > 0)).toBe(true);
    expect(labels()).toEqual(["#1 93%", "#2 lost"]);
  });

  it("hides boxes when the overlay is off and hides labels when labels are off", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), trackedHeader([trackFixture()]));
    canvasCalls.length = 0;
    ref.current!.setOverlay({ show: true, labels: false, selectedId: null });
    expect(ops("strokeRect")).toHaveLength(1);
    expect(labels()).toEqual([]);
    canvasCalls.length = 0;
    ref.current!.setOverlay({ show: false, labels: true, selectedId: null });
    expect(ops("drawImage")).toHaveLength(1);
    expect(ops("strokeRect")).toHaveLength(0);
  });

  it("highlights the selected track", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ track_id: 1 }), trackFixture({ track_id: 2, bbox: [300, 60, 380, 160] })]));
    canvasCalls.length = 0;
    ref.current!.setOverlay({ show: true, labels: true, selectedId: 2 });
    const strokes = ops("strokeStyle").map((c) => c.args[0]);
    expect(strokes).toContain("#ffffff");
    expect(strokes[strokes.length - 1]).toBe("#ffffff"); // selected is painted last (on top)
  });

  it("keeps boxes aligned in a letterboxed canvas", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ bbox: [100, 100, 200, 200] })], { frame_width: 400, frame_height: 400 }));
    expect(ops("strokeRect")[0]!.args).toEqual([175 + 112.5, 112.5, 112.5, 112.5]);
  });

  it("reports the clicked track (and null for empty space)", () => {
    const clicks: (Track | null)[] = [];
    const { ref, getByTestId } = mount((t) => clicks.push(t));
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ track_id: 5, bbox: [100, 100, 200, 200] })], { frame_width: 400, frame_height: 400 }));
    const canvas = getByTestId("video-canvas") as HTMLCanvasElement;
    canvas.getBoundingClientRect = () => ({ left: 0, top: 0, width: 800, height: 450, right: 800, bottom: 450, x: 0, y: 0, toJSON: () => ({}) });
    const user = userEvent.setup();
    return user
      .pointer({ target: canvas, coords: { clientX: 175 + 150, clientY: 150 }, keys: "[MouseLeft]" })
      .then(() => user.pointer({ target: canvas, coords: { clientX: 10, clientY: 10 }, keys: "[MouseLeft]" }))
      .then(() => {
        expect(clicks.map((t) => t?.track_id ?? null)).toEqual([5, null]);
      });
  });
});

// -------------------------------------------------------------------------- page
function open(c: ProcessingController = createController()) {
  installFakeStreaming();
  return { c, ...renderApp("/live", {}, processingFetch(c)) };
}
const push = (ws: FakeSocket, message: unknown) =>
  act(async () => {
    ws.message(typeof message === "string" || message instanceof ArrayBuffer ? message : JSON.stringify(message));
  });

async function running(statsOver: Record<string, number> = {}) {
  const c = createController();
  open(c);
  const user = userEvent.setup();
  await screen.findByText("Ready to start");
  await user.click(screen.getByTestId("start-button"));
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  const ws = FakeSocket.last;
  ws.open();
  await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING" }) });
  await push(ws, {
    type: "stats",
    stats: {
      ...emptyStats, frames_read: 90, frames_processed: 30, tracks_enabled: 1, tracks_confirmed: 2,
      tracks_tentative: 1, tracks_lost: 1, tracks_created_total: 9, tracks_confirmed_total: 4,
      tracks_rejected_total: 3, tracks_removed_total: 2, tracks_recovered_total: 1, tracking_ms_avg: 0.31,
      ...statsOver,
    },
  });
  return { ws, user, c };
}

const frameWith = (ws: FakeSocket, tracks: Track[], id = 12) =>
  push(ws, packFrame(trackedHeader(tracks, { frame_id: id })));

describe("tracking panel", () => {
  beforeEach(() => FakeSocket.reset());

  it("shows real counts from the session stats and never calls them visitors", async () => {
    const { ws, user } = await running();
    await frameWith(ws, [trackFixture()]);
    await waitFor(() => expect(screen.getByTestId("trk-confirmed")).toHaveTextContent("2"));
    expect(screen.getByTestId("trk-tentative")).toHaveTextContent("1");
    expect(screen.getByTestId("trk-lost")).toHaveTextContent("1");
    await user.click(screen.getByTestId("diagnostics-toggle"));
    expect(screen.getByTestId("trk-created")).toHaveTextContent("9");
    expect(screen.getByTestId("trk-confirmed-total")).toHaveTextContent("4");
    expect(screen.getByTestId("trk-rejected")).toHaveTextContent("3");
    expect(screen.getByTestId("trk-removed")).toHaveTextContent("2");
    expect(screen.getByTestId("trk-recovered")).toHaveTextContent("1");
    expect(screen.getByTestId("trk-ms")).toHaveTextContent("0.31 ms");
    expect(screen.getByTestId("trk-idsw")).toHaveTextContent(/not measurable/);
    const panel = screen.getByTestId("tracking-panel");
    expect(panel.textContent).not.toMatch(/unique visitors?:|visitors? count/i);
    expect(within(panel).getByText(/not visitor identities/)).toBeInTheDocument();
  });

  it("shows placeholders before any frame is analysed", async () => {
    open();
    await screen.findByText("Ready to start");
    expect(screen.getByTestId("trk-confirmed")).toHaveTextContent("—");
    expect(screen.getByTestId("track-list-empty")).toHaveTextContent(/appear once frames are analysed/i);
  });

  it("lists the tracks of the latest frame with their states", async () => {
    const { ws } = await running();
    await frameWith(ws, [
      trackFixture({ track_id: 3 }),
      trackFixture({ track_id: 5, state: "LOST", observed: false, detection_confidence: null, missed_updates: 2 }),
      trackFixture({ track_id: 6, state: "TENTATIVE" }),
    ]);
    const list = await screen.findByTestId("track-list");
    expect(within(list).getByTestId("track-chip-3")).toHaveTextContent(/#3\s*Confirmed/);
    expect(within(list).getByTestId("track-chip-5")).toHaveTextContent(/#5\s*Lost/);
    expect(within(list).getByTestId("track-chip-6")).toHaveTextContent(/#6\s*Tentative/);
    expect(screen.getByRole("img", { name: /3 tracks shown/ })).toBeInTheDocument();
  });

  it("opens, updates and closes track details", async () => {
    const { ws, user } = await running();
    await frameWith(ws, [trackFixture({ track_id: 3, first_seen_ms: 1200, last_seen_ms: 4800, age_ms: 3600 })]);
    await user.click(await screen.findByTestId("track-chip-3"));
    const details = await screen.findByTestId("track-details");
    expect(within(details).getByRole("heading", { name: "Track #3" })).toBeInTheDocument();
    expect(screen.getByTestId("detail-state")).toHaveTextContent("Confirmed");
    expect(screen.getByTestId("detail-first")).toHaveTextContent("1.2 s");
    expect(screen.getByTestId("detail-last")).toHaveTextContent("4.8 s");
    expect(screen.getByTestId("detail-age")).toHaveTextContent("3.6 s");
    expect(screen.getByTestId("detail-conf")).toHaveTextContent("93%");
    expect(screen.getByTestId("detail-box")).toHaveTextContent("100,50 – 200,150 (100×100)");
    expect(screen.getByTestId("detail-source")).toHaveTextContent("detected in this frame");
    expect(screen.getByTestId("track-chip-3")).toHaveAttribute("aria-pressed", "true");

    // The same track turns LOST: details follow the live data and say the box is predicted.
    await frameWith(ws, [trackFixture({ track_id: 3, state: "LOST", observed: false, detection_confidence: null, missed_updates: 2 })], 15);
    await waitFor(() => expect(screen.getByTestId("detail-state")).toHaveTextContent("Lost"));
    expect(screen.getByTestId("detail-source")).toHaveTextContent("predicted");
    expect(screen.getByTestId("detail-conf")).toHaveTextContent("93%"); // last OBSERVED value

    await user.click(screen.getByTestId("detail-close"));
    await waitFor(() => expect(screen.queryByTestId("track-details")).not.toBeInTheDocument());
    expect(screen.getByTestId("track-chip-3")).toHaveAttribute("aria-pressed", "false");
  });

  it("keeps the last known details when the selected track disappears", async () => {
    const { ws, user } = await running();
    await frameWith(ws, [trackFixture({ track_id: 3 })]);
    await user.click(await screen.findByTestId("track-chip-3"));
    await frameWith(ws, [trackFixture({ track_id: 4 })], 18);
    await waitFor(() => expect(screen.getByTestId("detail-ended")).toBeInTheDocument());
    expect(screen.getByTestId("detail-state")).toHaveTextContent("Ended");
    expect(screen.queryByTestId("track-chip-3")).not.toBeInTheDocument();
  });

  it("toggles the overlay and the labels on the canvas", async () => {
    const { ws, user } = await running();
    await frameWith(ws, [trackFixture()]);
    await waitFor(() => expect(labels()).toContain("#1 93%"));

    canvasCalls.length = 0;
    await user.click(screen.getByTestId("toggle-labels"));
    expect(screen.getByTestId("toggle-labels")).toHaveAttribute("aria-pressed", "false");
    expect(ops("strokeRect").length).toBeGreaterThan(0);
    expect(labels()).toEqual([]);

    canvasCalls.length = 0;
    await user.click(screen.getByTestId("toggle-overlay"));
    expect(screen.getByTestId("toggle-overlay")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("toggle-labels")).toBeDisabled();
    expect(ops("strokeRect")).toHaveLength(0);
    expect(ops("drawImage").length).toBeGreaterThan(0);

    await user.click(screen.getByTestId("toggle-overlay"));
    expect(ops("strokeRect").length).toBeGreaterThan(0);
  });

  it("selects a track from the chip and highlights it on the canvas", async () => {
    const { ws, user } = await running();
    await frameWith(ws, [trackFixture({ track_id: 1 }), trackFixture({ track_id: 2, bbox: [300, 60, 380, 160] })]);
    canvasCalls.length = 0;
    await user.click(await screen.findByTestId("track-chip-2"));
    expect(ops("strokeStyle").map((c) => c.args[0])).toContain("#ffffff");
  });

  it("explains when tracking is disabled on the server", async () => {
    const c = createController({ active: sessionFixture({ state: "RUNNING", settings: { model: "m", frame_skip: 2, confidence_threshold: 0.5, tracking_enabled: false } }) });
    c.current = c.active;
    open(c);
    expect(await screen.findByTestId("tracking-disabled")).toBeInTheDocument();
  });

  it("surfaces tracker errors reported by the worker", async () => {
    const { ws, user } = await running({ tracks_errors: 3 });
    await frameWith(ws, [trackFixture()]);
    await user.click(screen.getByTestId("diagnostics-toggle"));
    expect(screen.getByTestId("trk-errors")).toHaveTextContent("3");
  });

  it("clears the selection and track list when a new session starts", async () => {
    const { ws, user, c } = await running();
    await frameWith(ws, [trackFixture({ track_id: 3 })]);
    await user.click(await screen.findByTestId("track-chip-3"));
    expect(await screen.findByTestId("track-details")).toBeInTheDocument();
    await push(ws, { type: "ended", session: sessionFixture({ state: "STOPPED", stop_reason: "user" }) });
    c.startResponse = () =>
      new Response(JSON.stringify(sessionFixture({ id: "b".repeat(32) })), { status: 202, headers: { "Content-Type": "application/json" } });
    await waitFor(() => expect(screen.getByTestId("start-button")).toBeEnabled());
    await user.click(screen.getByTestId("start-button"));
    await waitFor(() => expect(screen.queryByTestId("track-details")).not.toBeInTheDocument());
    expect(screen.queryByTestId("track-chip-3")).not.toBeInTheDocument();
    expect(SID).toBeTruthy();
  });
});
