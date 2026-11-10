import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import {
  FakeSocket,
  apiError,
  createController,
  emptyStats,
  frameHeader,
  installFakeStreaming,
  packFrame,
  processingFetch,
  sessionFixture,
  sourcesFixture,
  type ProcessingController,
} from "./monitoring-mocks";
import { renderApp } from "./render";
import { canvasCalls } from "./setup";

const SID = "a".repeat(32);

function open(c: ProcessingController = createController()) {
  installFakeStreaming();
  const utils = renderApp("/live", {}, processingFetch(c));
  return { c, ...utils };
}

const overlay = () => screen.findByTestId("viewport-overlay");
const startButton = () => screen.getByTestId("start-button");

async function startSession(c: ProcessingController) {
  const user = userEvent.setup();
  await screen.findByText("Ready to start");
  await user.click(startButton());
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  return { user, ws: FakeSocket.last, c };
}

const push = (ws: FakeSocket, message: unknown) =>
  act(async () => {
    ws.message(typeof message === "string" || message instanceof ArrayBuffer ? message : JSON.stringify(message));
  });

beforeEach(() => FakeSocket.reset());

describe("preconditions and empty states", () => {
  it("lists configured sources and enables Start when everything is ready", async () => {
    open();
    expect(await screen.findByText("Ready to start")).toBeInTheDocument();
    const group = screen.getByRole("radiogroup", { name: "Video source" });
    expect(within(group).getAllByRole("radio")).toHaveLength(3);
    expect(screen.getByRole("radio", { name: /Sample: demo \(Video file\)/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Front door camera \(RTSP camera\)/ })).toBeEnabled();
    expect(startButton()).toBeEnabled();
  });

  it("disables unavailable sources and explains why", async () => {
    open();
    await screen.findByText("Ready to start");
    expect(screen.getByRole("radio", { name: /Missing clip/ })).toBeDisabled();
    expect(screen.getByText(/The video file was not found on the server/)).toBeInTheDocument();
  });

  it("never shows server paths or RTSP addresses", async () => {
    open();
    await screen.findByText("Ready to start");
    expect(document.body.textContent).not.toMatch(/rtsp:\/\//i);
    expect(document.body.textContent).not.toMatch(/[A-Z]:\\|datasets\//);
  });

  it("explains a missing worker and blocks Start", async () => {
    open(createController({ sources: sourcesFixture({ worker: { connected: false, worker_id: null, model_available: null, model_detail: null } }) }));
    expect(await screen.findByRole("heading", { name: "Video worker not connected" })).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
    expect(screen.getByText("The video worker is not connected.")).toBeInTheDocument();
    expect(within(await overlay()).getByText(/start-worker\.ps1/)).toBeInTheDocument();
  });

  it("explains an unavailable model", async () => {
    open(createController({ sources: sourcesFixture({ worker: { connected: true, worker_id: "w", model_available: false, model_detail: "missing" } }) }));
    expect(await screen.findByRole("heading", { name: "Face detection model unavailable" })).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
    expect(within(await overlay()).getByText(/download-model\.ps1/)).toBeInTheDocument();
  });

  it("explains that no source is configured", async () => {
    open(createController({ sources: sourcesFixture({ sources: [] }) }));
    expect(await screen.findByRole("heading", { name: "No video source configured" })).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
  });

  it("shows a backend-unreachable state instead of hanging", async () => {
    open(createController({ sources: "error" }));
    expect(await screen.findByRole("heading", { name: "Backend not reachable" })).toBeInTheDocument();
    expect(startButton()).toBeDisabled();
  });

  it("shows placeholders - not invented numbers - before any processing", async () => {
    open();
    await screen.findByText("Ready to start");
    for (const id of ["stat-faces", "stat-fps", "stat-read", "stat-processed", "stat-skipped", "stat-inference"]) {
      expect(screen.getByTestId(id)).toHaveTextContent("—");
    }
    expect(screen.getByTestId("frame-info")).toHaveTextContent("No frames received yet");
  });
});

describe("start, live updates and stop", () => {
  it("only reports Processing after the backend confirms RUNNING", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    expect(c.calls.find((x) => x.method === "POST")?.body).toEqual({ source_id: "sample-demo" });
    expect(ws.url).toContain(`/sessions/${SID}/stream`);
    // Server accepted the request, but the worker has not confirmed yet.
    expect(await screen.findByRole("heading", { name: "Connecting to the source…" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Processing" })).not.toBeInTheDocument();

    ws.open();
    await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING", connection: "connected" }) });
    await waitFor(() => expect(screen.getByTestId("viewport")).toHaveAttribute("data-view", "processing"));
    expect(screen.getByTestId("status-announcement")).toHaveTextContent(/^Processing\./);
  });

  it("draws streamed frames, shows frame info and real statistics", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING" }) });
    await push(
      ws,
      {
        type: "stats",
        stats: {
          ...emptyStats, frames_read: 90, frames_processed: 30, frames_skipped: 60,
          faces_last_frame: 1, processing_fps: 8.4, avg_inference_ms: 22.5, last_inference_ms: 24,
          frame_width: 640, frame_height: 360, source_fps: 25, detections_total: 28, max_faces_in_frame: 1,
        },
      },
    );
    await push(ws, packFrame(frameHeader({ frame_id: 12, timestamp_ms: 480 })));

    await waitFor(() => expect(canvasCalls.some((x) => x.op === "strokeRect")).toBe(true));
    const stroke = canvasCalls.find((x) => x.op === "strokeRect")!.args as number[];
    expect(stroke).toHaveLength(4);
    expect(canvasCalls.find((x) => x.op === "fillText")!.args[0]).toBe("93%");

    expect(screen.getByTestId("stat-read")).toHaveTextContent("90");
    expect(screen.getByTestId("stat-processed")).toHaveTextContent("30");
    expect(screen.getByTestId("stat-skipped")).toHaveTextContent("60");
    expect(screen.getByTestId("stat-fps")).toHaveTextContent("8.4");
    expect(screen.getByTestId("stat-inference")).toHaveTextContent("22.5 ms");
    await waitFor(() => expect(screen.getByTestId("frame-info")).toHaveTextContent("Frame #12"));
    await waitFor(() => expect(screen.getByTestId("stat-faces")).toHaveTextContent("1"));
    expect(screen.getByTestId("confidence-line")).toHaveTextContent("Highest detection confidence in latest frame: 93%");
    expect(screen.getByRole("img", { name: /1 face detected/ })).toBeInTheDocument();
  });

  it("never labels any value as unique visitors", async () => {
    const { c } = open();
    const { ws, user } = await startSession(c);
    ws.open();
    await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING", stats: { ...emptyStats, frames_read: 5, detections_total: 40 } }) });
    await user.click(await screen.findByRole("button", { name: /Detection details/ }));
    expect(screen.getByRole("button", { name: /Detection details/ })).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByText(/not a count of visitors/)).toBeInTheDocument();
    // The only mention of visitors is the explicit disclaimer; no stat or label claims a visitor count.
    const labels = Array.from(document.querySelectorAll("dt")).map((n) => n.textContent ?? "");
    expect(labels.join("|")).not.toMatch(/visitor/i);
    expect(screen.getByText("Face detections (sum)")).toBeInTheDocument();
  });

  it("locks the source while processing and stops on request", async () => {
    const { c } = open();
    c.stopResponse = () =>
      new Response(JSON.stringify(sessionFixture({ state: "STOPPING" })), { status: 200, headers: { "Content-Type": "application/json" } });
    const { ws, user } = await startSession(c);
    ws.open();
    await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING" }) });
    expect(await screen.findByTestId("stop-button")).toBeEnabled();
    expect(screen.getByRole("radio", { name: /Front door camera/ })).toBeDisabled();

    await user.click(screen.getByTestId("stop-button"));
    await waitFor(() => expect(c.calls.some((x) => x.path === `/sessions/${SID}/stop`)).toBe(true));
    expect(await screen.findByRole("heading", { name: "Stopping…" })).toBeInTheDocument();

    await push(ws, { type: "ended", session: sessionFixture({ state: "STOPPED", stop_reason: "user_requested" }) });
    expect(await screen.findByText("Processing stopped")).toBeInTheDocument();
    expect(screen.getByTestId("start-button")).toBeEnabled();
    await waitFor(() => expect(ws.closed).toBe(true));
  });

  it("explains the outcome in a banner that keeps the last frame visible", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING" }) });
    await push(ws, packFrame(frameHeader({ frame_id: 6 })));
    await waitFor(() => expect(canvasCalls.some((x) => x.op === "drawImage")).toBe(true));
    await waitFor(() => expect(screen.getByTestId("frame-info")).toHaveTextContent("Frame #6"));
    expect(screen.queryByTestId("viewport-banner")).not.toBeInTheDocument(); // nothing to explain while running
    await push(ws, { type: "ended", session: sessionFixture({ state: "STOPPED", stop_reason: "user_requested" }) });
    const banner = await screen.findByTestId("viewport-banner");
    expect(within(banner).getByRole("heading", { name: "Processing stopped" })).toBeInTheDocument();
    // Regression: the leaving overlay and the entering banner used to show the same heading together.
    expect(screen.getAllByRole("heading", { name: "Processing stopped" })).toHaveLength(1);
    await waitFor(() => expect(screen.queryByTestId("viewport-overlay")).not.toBeInTheDocument()); // picture not covered
    expect(screen.getByTestId("frame-info")).toHaveTextContent("Frame #6");
  });

  it("shows completion when the video ends", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, { type: "ended", session: sessionFixture({ state: "COMPLETED", stats: { ...emptyStats, frames_read: 100 } }) });
    expect(await screen.findByText("Processing completed")).toBeInTheDocument();
    expect(screen.getByTestId("stat-read")).toHaveTextContent("100");
  });

  it("is operable by keyboard", async () => {
    const { c } = open();
    installFakeStreaming();
    const user = userEvent.setup();
    await screen.findByText("Ready to start");
    startButton().focus();
    expect(startButton()).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(c.calls.some((x) => x.method === "POST" && x.path === "/sessions")).toBe(true));
  });

  it("lets the user choose a different source with the keyboard", async () => {
    open();
    const user = userEvent.setup();
    await screen.findByText("Ready to start");
    screen.getByRole("radio", { name: /Sample: demo/ }).focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("radio", { name: /Front door camera/ })).toBeChecked();
  });
});

describe("failures", () => {
  it("shows the backend's reason when starting is refused and offers Retry", async () => {
    const { c } = open();
    c.startResponse = () => apiError(503, "worker_unavailable", "The video worker is not connected.");
    const user = userEvent.setup();
    await screen.findByText("Ready to start");
    await user.click(startButton());
    expect(await screen.findByRole("heading", { name: "Video worker not connected" })).toBeInTheDocument();
    expect(FakeSocket.instances).toHaveLength(0); // no stream for a session that never existed
    // Retry succeeds once the problem is fixed.
    c.startResponse = () => new Response(JSON.stringify(sessionFixture()), { status: 202, headers: { "Content-Type": "application/json" } });
    await user.click(screen.getByTestId("retry-button"));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    expect(await screen.findByRole("heading", { name: "Connecting to the source…" })).toBeInTheDocument();
  });

  it("reports a camera that cannot be reached", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, {
      type: "ended",
      session: sessionFixture({ state: "ERROR", error: { code: "camera_unavailable", message: "unreachable" } }),
    });
    expect(await screen.findByRole("heading", { name: "Camera disconnected" })).toBeInTheDocument();
    expect(screen.getByTestId("retry-button")).toBeInTheDocument();
    expect(within(await overlay()).getByText(/Check its power, network and address/)).toBeInTheDocument();
  });

  it("reports a stream that is reconnecting while still running", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, { type: "state", session: sessionFixture({ state: "RUNNING", connection: "reconnecting" }) });
    expect(await screen.findByRole("heading", { name: "Camera disconnected - reconnecting" })).toBeInTheDocument();
    expect(screen.getByTestId("stop-button")).toBeEnabled(); // can always give up
  });

  it("reports a model failure raised by the worker", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, { type: "ended", session: sessionFixture({ state: "ERROR", error: { code: "model_unavailable", message: "x" } }) });
    expect(await screen.findByRole("heading", { name: "Face detection model unavailable" })).toBeInTheDocument();
  });

  it("reports generic processing errors with the server's message", async () => {
    const { c } = open();
    const { ws } = await startSession(c);
    ws.open();
    await push(ws, { type: "ended", session: sessionFixture({ state: "ERROR", error: { code: "inference_failed", message: "Inference failed on frame 12." } }) });
    expect(await screen.findByRole("heading", { name: "Processing error" })).toBeInTheDocument();
    expect(screen.getByText("Inference failed on frame 12.")).toBeInTheDocument();
  });

  it("converges on the backend state by polling when the socket gives no updates", async () => {
    const { c } = open();
    await startSession(c);
    c.current = sessionFixture({ state: "RUNNING" });
    // The hook polls every 2 s while a session is active.
    await waitFor(() => expect(screen.getByTestId("viewport")).toHaveAttribute("data-view", "processing"), { timeout: 5000 });
  });
});

describe("returning to the page", () => {
  it("re-attaches to a session that kept running in the background", async () => {
    installFakeStreaming();
    const c = createController({ active: sessionFixture({ state: "RUNNING" }), current: sessionFixture({ state: "RUNNING" }) });
    renderApp("/live", {}, processingFetch(c));
    await waitFor(() => expect(screen.getByTestId("viewport")).toHaveAttribute("data-view", "processing"));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    expect(screen.getByTestId("stop-button")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: /Sample: demo/ })).toBeDisabled();
    expect(screen.getByTestId("viewport-source")).toHaveTextContent("Sample: demo");
  });

  it("closes its stream when the page is left", async () => {
    installFakeStreaming();
    const c = createController({ active: sessionFixture({ state: "RUNNING" }), current: sessionFixture({ state: "RUNNING" }) });
    const { router } = renderApp("/live", {}, processingFetch(c));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    await act(async () => {
      await router.navigate("/settings");
    });
    await waitFor(() => expect(FakeSocket.instances[0]!.closed).toBe(true));
  });
});
