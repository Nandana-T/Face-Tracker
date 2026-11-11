import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "@/components/ui/toaster";
import { VideoCanvas, type VideoCanvasHandle } from "@/features/monitoring/video-canvas";
import type { TrackIdentity } from "@/lib/api-types";
import { identityColor, identityLabel, noticeFor, noticeKey } from "@/lib/identity";
import {
  FakeSocket,
  createController,
  emptyStats,
  installFakeStreaming,
  packFrame,
  processingFetch,
  sessionFixture,
  sourcesFixture,
  trackFixture,
  trackedHeader,
} from "./monitoring-mocks";
import { renderApp } from "./render";
import { canvasCalls } from "./setup";
import { identityEvent } from "./visitors-mocks";

const SID = "a".repeat(32);
const bitmap = () => ({ width: 640, height: 360, close: vi.fn() }) as unknown as ImageBitmap;
const labels = () => canvasCalls.filter((c) => c.op === "fillText").map((c) => c.args[0]);
const strokes = () => canvasCalls.filter((c) => c.op === "strokeStyle").map((c) => c.args[0]);

const rec = (visitor: boolean): TrackIdentity => ({
  state: visitor ? "RECOGNIZED" : "MATCH_PENDING",
  visitor: visitor ? { id: "b".repeat(32), number: 124, label: "V00124" } : null,
});

describe("identity labels (pure)", () => {
  it("uses the persistent visitor label for recognised and newly registered faces", () => {
    expect(identityLabel({ track_id: 7 }, rec(true))).toBe("V00124 · #7");
    expect(
      identityLabel({ track_id: 7 }, { state: "REGISTERED", visitor: { id: "x", number: 124, label: "V00124" } }),
    ).toBe("V00124 new · #7");
  });

  it("describes undecided, low-quality, ambiguous and unverified tracks honestly", () => {
    expect(identityLabel({ track_id: 7 }, undefined)).toBe("#7 · Recognizing");
    expect(identityLabel({ track_id: 7 }, rec(false))).toBe("#7 · Recognizing");
    expect(identityLabel({ track_id: 15 }, { state: "INSUFFICIENT_QUALITY", visitor: null })).toBe("#15 · Low quality");
    expect(identityLabel({ track_id: 3 }, { state: "AMBIGUOUS", visitor: null })).toBe("#3 · Ambiguous");
    expect(identityLabel({ track_id: 3 }, { state: "REJECTED", visitor: null })).toBe("#3 · Unverified");
    // a visitor label is never shown for a state that does not carry one
    expect(identityLabel({ track_id: 3 }, { state: "AMBIGUOUS", visitor: { id: "x", number: 1, label: "V00001" } })).toBe("#3 · Ambiguous");
  });

  it("colours recognised/registered/ambiguous distinctly and leaves other states to the track colour", () => {
    const colors = ["RECOGNIZED", "REGISTERED", "AMBIGUOUS"].map((s) =>
      identityColor({ state: s as TrackIdentity["state"], visitor: null }),
    );
    expect(new Set(colors).size).toBe(3);
    expect(identityColor({ state: "MATCH_PENDING", visitor: null })).toBeNull();
    expect(identityColor(undefined)).toBeNull();
  });

  it("creates a notice only for meaningful transitions", () => {
    expect(noticeFor(identityEvent())?.title).toBe("New visitor registered: V00124");
    expect(noticeFor(identityEvent({ event: "recognized", state: "RECOGNIZED" }))?.level).toBe("info");
    expect(noticeFor(identityEvent({ event: "ambiguous", state: "AMBIGUOUS", visitor: null }))?.level).toBe("warning");
    expect(noticeFor(identityEvent({ event: "failed", state: "UNRESOLVED", visitor: null }))?.level).toBe("error");
    expect(noticeFor(identityEvent({ event: "state", state: "INSUFFICIENT_QUALITY", visitor: null }))).toBeNull();
    expect(noticeFor(identityEvent({ event: "conflict", state: "RECOGNIZED" }))).toBeNull();
    expect(noticeKey(identityEvent())).toBe(noticeKey(identityEvent()));
    expect(noticeKey(identityEvent({ track_id: 2 }))).not.toBe(noticeKey(identityEvent()));
  });
});

describe("canvas recognition labels", () => {
  beforeEach(() => {
    Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 800 });
    Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 450 });
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      cb(0);
      return 1;
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
    Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  });

  function mount() {
    const ref = createRef<VideoCanvasHandle>();
    render(<VideoCanvas ref={ref} label="preview" />);
    canvasCalls.length = 0;
    return ref;
  }

  it("keeps plain tracking labels while recognition is not running", () => {
    const ref = mount();
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ track_id: 7 })]));
    expect(labels()).toEqual(["#7 93%"]);
  });

  it("shows 'Recognizing' for a confirmed track until the backend decides", () => {
    const ref = mount();
    ref.current!.setIdentities(new Map());
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ track_id: 7 })]));
    expect(labels()).toEqual(["#7 · Recognizing"]);
  });

  it("shows the visitor label and colour once recognised, without touching other tracks", () => {
    const ref = mount();
    ref.current!.setIdentities(new Map([[7, rec(true)]]));
    ref.current!.draw(
      bitmap(),
      trackedHeader([trackFixture({ track_id: 7 }), trackFixture({ track_id: 8, bbox: [300, 60, 380, 160] })]),
    );
    expect(labels()).toEqual(["V00124 · #7", "#8 · Recognizing"]);
    expect(strokes()).toContain("#a78bfa");
  });

  it("never labels predicted, lost or tentative boxes with an identity", () => {
    const ref = mount();
    ref.current!.setIdentities(new Map([[7, rec(true)]]));
    ref.current!.draw(
      bitmap(),
      trackedHeader([trackFixture({ track_id: 7, state: "LOST", observed: false, detection_confidence: null })]),
    );
    expect(labels()).toEqual(["#7 lost"]);
  });

  it("repaints when identities change (no new frame needed)", () => {
    const ref = mount();
    ref.current!.setIdentities(new Map());
    ref.current!.draw(bitmap(), trackedHeader([trackFixture({ track_id: 7 })]));
    canvasCalls.length = 0;
    ref.current!.setIdentities(new Map([[7, { state: "REGISTERED", visitor: { id: "x", number: 5, label: "V00005" } }]]));
    expect(labels()).toEqual(["V00005 new · #7"]);
  });
});

// ------------------------------------------------------------------------------ page integration
async function running(over: Parameters<typeof sessionFixture>[0] = {}, sources = sourcesFixture()) {
  installFakeStreaming();
  const c = createController({ sources });
  renderApp("/live", {}, processingFetch(c));
  const user = userEvent.setup();
  await screen.findByText("Ready to start");
  await user.click(screen.getByTestId("start-button"));
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  const ws = FakeSocket.last;
  ws.open();
  const push = (m: unknown) =>
    act(async () => {
      ws.message(typeof m === "string" || m instanceof ArrayBuffer ? m : JSON.stringify(m));
    });
  await push({
    type: "state",
    session: sessionFixture({ state: "RUNNING", settings: { model: "m", frame_skip: 2, confidence_threshold: 0.5, tracking_enabled: true, recognition_enabled: true }, ...over }),
  });
  await push({ type: "stats", stats: { ...emptyStats, frames_read: 30, frames_processed: 10, tracks_enabled: 1, recog_enabled: 1, recog_processed: 12, recog_avg_ms: 31.4 } });
  return { ws, user, push, c };
}

describe("live recognition", () => {
  let success: ReturnType<typeof vi.spyOn>;
  let warning: ReturnType<typeof vi.spyOn>;
  let error: ReturnType<typeof vi.spyOn>;
  let message: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    FakeSocket.reset();
    success = vi.spyOn(toast, "success").mockImplementation(() => "id");
    warning = vi.spyOn(toast, "warning").mockImplementation(() => "id");
    error = vi.spyOn(toast, "error").mockImplementation(() => "id");
    message = vi.spyOn(toast, "message").mockImplementation(() => "id");
  });
  afterEach(() => vi.restoreAllMocks());

  it("notifies once when a visitor is registered, even if the event arrives twice", async () => {
    const { push } = await running();
    await push(identityEvent({ session_id: SID, track_id: 4 }));
    await push(identityEvent({ session_id: SID, track_id: 4 }));
    expect(success).toHaveBeenCalledTimes(1);
    expect(success.mock.calls[0]![0]).toBe("New visitor registered: V00124");
    await waitFor(() => expect(screen.getByTestId("rec-registered")).toHaveTextContent("1"));
  });

  it("notifies for recognition, ambiguity and failure with the right severity", async () => {
    const { push } = await running();
    await push(identityEvent({ session_id: SID, track_id: 1, event: "recognized", state: "RECOGNIZED" }));
    await push(identityEvent({ session_id: SID, track_id: 2, event: "ambiguous", state: "AMBIGUOUS", visitor: null }));
    await push(identityEvent({ session_id: SID, track_id: 3, event: "failed", state: "UNRESOLVED", visitor: null }));
    expect(message).toHaveBeenCalledTimes(1);
    expect(warning).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledTimes(1);
    expect(success).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("rec-recognized")).toHaveTextContent("1"));
    expect(screen.getByTestId("rec-flagged")).toHaveTextContent("1");
  });

  it("stays silent for state changes without a transition and for a reconnect replay", async () => {
    const { push } = await running();
    await push(identityEvent({ session_id: SID, track_id: 5, event: "state", state: "INSUFFICIENT_QUALITY", visitor: null }));
    await push({ type: "identities", identities: [identityEvent({ session_id: SID, track_id: 6, event: "state", state: "REGISTERED" })] });
    expect(success).not.toHaveBeenCalled();
    expect(message).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByTestId("rec-registered")).toHaveTextContent("1")); // the state is still applied
    expect(screen.getByTestId("rec-flagged")).toHaveTextContent("1");
  });

  it("ignores identity events that belong to another session", async () => {
    const { push } = await running();
    await push(identityEvent({ session_id: "f".repeat(32), track_id: 4 }));
    expect(success).not.toHaveBeenCalled();
    expect(screen.getByTestId("rec-registered")).toHaveTextContent("0");
  });

  it("shows the visitor on the track chip and in the track details", async () => {
    const { push, ws, user } = await running();
    await push(identityEvent({ session_id: SID, track_id: 4, event: "recognized", state: "RECOGNIZED" }));
    await push(packFrame(trackedHeader([trackFixture({ track_id: 4 })], { frame_id: 9 })));
    expect(await screen.findByTestId("chip-visitor-4")).toHaveTextContent("V00124");
    await user.click(screen.getByTestId("track-chip-4"));
    expect(await screen.findByTestId("detail-visitor")).toHaveTextContent("V00124");
    expect(ws).toBeTruthy();
  });

  it("keeps track ids and visitor ids visibly distinct and never claims a visitor count", async () => {
    const { push } = await running();
    await push(identityEvent({ session_id: SID, track_id: 4 }));
    const panel = screen.getByTestId("recognition-panel");
    expect(panel).toHaveTextContent("Visitor labels (V…) are persistent; track labels (#…) are temporary.");
    expect(panel.textContent).not.toMatch(/unique visitors?|visitor count|visits/i);
    expect(within(panel).getByTestId("rec-cost")).toHaveTextContent("12 faces analysed · 31 ms each");
    expect(screen.getByTestId("open-visitors")).toHaveAttribute("href", "/visitors");
  });

  it("explains when the worker has no recognition model", async () => {
    installFakeStreaming();
    const sources = sourcesFixture({
      worker: {
        connected: true, worker_id: "w", model_available: true, model_detail: null,
        recognition_available: false, recognition_detail: "The face recognition model is missing or corrupt on the worker machine.",
      },
    });
    renderApp("/live", {}, processingFetch(createController({ sources })));
    expect(await screen.findByTestId("recognition-unavailable")).toHaveTextContent("missing or corrupt");
    expect(screen.getByTestId("rec-recognized")).toHaveTextContent("—");
  });

  it("does not carry identities into the next session", async () => {
    const { push, user, c } = await running();
    await push(identityEvent({ session_id: SID, track_id: 4 }));
    await waitFor(() => expect(screen.getByTestId("rec-registered")).toHaveTextContent("1"));
    await push({ type: "ended", session: sessionFixture({ state: "STOPPED", stop_reason: "user" }) });
    c.startResponse = () =>
      new Response(JSON.stringify(sessionFixture({ id: "b".repeat(32) })), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    await waitFor(() => expect(screen.getByTestId("start-button")).toBeEnabled());
    await user.click(screen.getByTestId("start-button"));
    await waitFor(() => expect(FakeSocket.instances.length).toBe(2));
    // track #4 of the old session means nothing here: no stale visitor, no stale count
    expect(screen.getByTestId("rec-registered")).not.toHaveTextContent("1");
    expect(screen.queryByTestId("chip-visitor-4")).not.toBeInTheDocument();
  });
});
