import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "@/components/ui/toaster";
import { liveEvent, visitId } from "./events-mocks";
import {
  FakeSocket,
  createController,
  emptyStats,
  installFakeStreaming,
  processingFetch,
  sessionFixture,
  sourcesFixture,
} from "./monitoring-mocks";
import { renderApp } from "./render";

const SID = "a".repeat(32);

async function running(recognition = true) {
  installFakeStreaming();
  const c = createController({ sources: sourcesFixture() });
  renderApp("/live", {}, processingFetch(c));
  const user = userEvent.setup();
  await screen.findByText("Ready to start");
  await user.click(screen.getByTestId("start-button"));
  await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
  const ws = FakeSocket.last;
  ws.open();
  const push = (m: unknown) => act(async () => ws.message(JSON.stringify(m)));
  await push({
    type: "state",
    session: sessionFixture({
      state: "RUNNING",
      settings: {
        model: "m",
        frame_skip: 2,
        confidence_threshold: 0.5,
        tracking_enabled: true,
        recognition_enabled: recognition,
      },
    }),
  });
  await push({ type: "stats", stats: { ...emptyStats, frames_read: 30, frames_processed: 10, tracks_enabled: 1 } });
  return { ws, push, user, c };
}

const ev = (n: number, type: "entry" | "exit" | "interrupted" = "entry", over = {}) =>
  liveEvent(n, type, { session_id: SID, ...over });

describe("live entry/exit events", () => {
  let success: ReturnType<typeof vi.spyOn>;
  let warning: ReturnType<typeof vi.spyOn>;
  let message: ReturnType<typeof vi.spyOn>;
  beforeEach(() => {
    FakeSocket.reset();
    success = vi.spyOn(toast, "success").mockImplementation(() => "id");
    warning = vi.spyOn(toast, "warning").mockImplementation(() => "id");
    message = vi.spyOn(toast, "message").mockImplementation(() => "id");
  });
  afterEach(() => vi.restoreAllMocks());

  it("notifies once per event id even when the message is delivered twice", async () => {
    const { push } = await running();
    await push(ev(1, "entry"));
    await push(ev(1, "entry"));
    await push(ev(1, "entry"));
    expect(success).toHaveBeenCalledTimes(1);
    expect(success.mock.calls[0]![0]).toBe("V00001 entered");
    expect(screen.getAllByTestId("visit-recent-item")).toHaveLength(1);
  });

  it("shows who is inside, and clears them when the exit is recorded", async () => {
    const { push } = await running();
    expect(screen.getByTestId("visit-active-count")).toHaveTextContent("0");
    await push(ev(1, "entry"));
    await push(ev(2, "entry"));
    await waitFor(() => expect(screen.getByTestId("visit-active-count")).toHaveTextContent("2"));
    await push(ev(1, "exit"));
    await waitFor(() => expect(screen.getByTestId("visit-active-count")).toHaveTextContent("1"));
    expect(message).toHaveBeenCalledTimes(1);
    expect(message.mock.calls[0]![0]).toBe("V00001 left the view");
    const items = screen.getAllByTestId("visit-recent-item");
    expect(items[0]).toHaveTextContent("Exit");
    expect(items[0]).toHaveTextContent("V00001");
  });

  it("reflects temporary absence and recovery from visit_state messages", async () => {
    const { push } = await running();
    await push(ev(1, "entry"));
    await push({ type: "visit_state", visit_id: visitId(1), visitor_id: "x", visitor_number: 1, state: "TEMPORARILY_MISSING" });
    await waitFor(() => expect(screen.getByTestId("visit-waiting-count")).toHaveTextContent("1"));
    expect(screen.getByTestId("visit-active-count")).toHaveTextContent("0");
    await push({ type: "visit_state", visit_id: visitId(1), visitor_id: "x", visitor_number: 1, state: "ACTIVE" });
    await waitFor(() => expect(screen.getByTestId("visit-active-count")).toHaveTextContent("1"));
    expect(success).toHaveBeenCalledTimes(1); // no extra notification for a recovery
  });

  it("restores the open visits for a browser that connects mid-session, silently", async () => {
    const { push } = await running();
    await push({
      type: "visits",
      visits: [
        { visit_id: visitId(3), visitor_id: "x", visitor_number: 3, state: "ACTIVE" },
        { visit_id: visitId(4), visitor_id: "y", visitor_number: 4, state: "TEMPORARILY_MISSING" },
      ],
    });
    await waitFor(() => expect(screen.getByTestId("visit-active-count")).toHaveTextContent("1"));
    expect(screen.getByTestId("visit-waiting-count")).toHaveTextContent("1");
    expect(success).not.toHaveBeenCalled();
  });

  it("describes an interrupted visit as a warning and an ended observation without saying it left", async () => {
    const { push } = await running();
    await push(ev(1, "entry"));
    await push(ev(2, "entry"));
    await push(ev(1, "exit", { exit_basis: "observation_ended" }));
    await push(ev(2, "interrupted"));
    expect(message.mock.calls[0]![0]).toBe("V00001: observation ended");
    expect(warning).toHaveBeenCalledTimes(1);
    expect(warning.mock.calls[0]![0]).toBe("V00002: visit interrupted");
    await waitFor(() => expect(screen.getByTestId("visit-active-count")).toHaveTextContent("0"));
    const recent = screen.getByTestId("visit-recent");
    expect(within(recent).getAllByTestId("visit-recent-item")[0]).toHaveTextContent("Interrupted");
  });

  it("links recent events to the visit and warns about recording problems without hiding the event", async () => {
    const { push } = await running();
    await push(ev(1, "entry", { persistence_status: "DEAD" }));
    expect(await screen.findByTestId("visit-problem")).toHaveTextContent(/recording problem/);
    expect(screen.getByRole("link", { name: /Open the visit of V00001/ })).toHaveAttribute(
      "href",
      `/events?visit=${visitId(1)}`,
    );
    expect(screen.getByTestId("open-events")).toHaveAttribute("href", "/events");
  });

  it("ignores events of another session and keeps the list bounded", async () => {
    const { push } = await running();
    await push(liveEvent(1, "entry", { session_id: "f".repeat(32) }));
    expect(success).not.toHaveBeenCalled();
    for (let i = 1; i <= 12; i++) await push(ev(i, "entry"));
    // (items that left the list may linger a moment while their exit animation finishes)
    await waitFor(() => expect(screen.getAllByTestId("visit-recent-item")).toHaveLength(8));
    expect(screen.getAllByTestId("visit-recent-item")[0]).toHaveTextContent("V00012");
  });

  it("does not carry visits into the next session", async () => {
    const { push, user, c } = await running();
    await push(ev(1, "entry"));
    await waitFor(() => expect(screen.getByTestId("visit-active-count")).toHaveTextContent("1"));
    await push({ type: "ended", session: sessionFixture({ state: "STOPPED", stop_reason: "user" }) });
    c.startResponse = () =>
      new Response(JSON.stringify(sessionFixture({ id: "b".repeat(32) })), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    await waitFor(() => expect(screen.getByTestId("start-button")).toBeEnabled());
    await user.click(screen.getByTestId("start-button"));
    await waitFor(() => expect(FakeSocket.instances.length).toBe(2));
    expect(screen.getByTestId("visit-active-count")).not.toHaveTextContent("1");
    expect(screen.getByTestId("visit-recent-empty")).toBeInTheDocument();
  });

  it("states honestly that visits need recognition and never shows a unique-visitor count", async () => {
    await running(false);
    expect(screen.getByTestId("visit-panel-off")).toHaveTextContent(/need face recognition/);
    expect(screen.getByTestId("visit-active-count")).toHaveTextContent("—");
    expect(screen.getByTestId("visit-panel").textContent).not.toMatch(/unique visitors?|total visitors/i);
  });
});
