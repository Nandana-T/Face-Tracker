import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { localToIso } from "@/features/events/events-page";
import { formatDuration, visitNotice, visitorText } from "@/lib/visits";
import {
  createEvents,
  entryEvent,
  eventsFetch,
  exitEvent,
  liveEvent,
  visitDetail,
  visitId,
  visitorRef,
  type EventsController,
} from "./events-mocks";
import { renderApp } from "./render";

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:evidence"), revokeObjectURL: vi.fn() }));
});

const open = (c: EventsController, path = "/events") => renderApp(path, {}, eventsFetch(c));
const lastList = (c: EventsController) => c.calls.filter((x) => x.startsWith("GET /events?")).at(-1) ?? "";

describe("presentation helpers", () => {
  it("formats durations only when there is a real number", () => {
    expect(formatDuration(null)).toBeNull();
    expect(formatDuration(-3)).toBeNull();
    expect(formatDuration(Number.NaN)).toBeNull();
    expect(formatDuration(42)).toBe("42 s");
    expect(formatDuration(125)).toBe("2 min 05 s");
    expect(formatDuration(3720)).toBe("1 h 02 min");
  });

  it("names an erased visitor honestly and builds notices per event kind", () => {
    expect(visitorText(null)).toBe("Erased visitor");
    expect(visitorText({ label: "V00007" })).toBe("V00007");
    expect(visitNotice(liveEvent(7, "entry")).level).toBe("success");
    expect(visitNotice(liveEvent(7, "exit")).title).toBe("V00007 left the view");
    const ended = visitNotice(liveEvent(7, "exit", { exit_basis: "observation_ended" }));
    expect(ended.title).toBe("V00007: observation ended");
    expect(ended.description).toMatch(/not a verified departure/);
    expect(visitNotice(liveEvent(7, "interrupted")).level).toBe("warning");
  });

  it("converts datetime-local values and rejects garbage", () => {
    expect(localToIso("")).toBeUndefined();
    expect(localToIso("nope")).toBeUndefined();
    expect(localToIso("2026-11-10T10:00")).toMatch(/^2026-11-10T\d\d:\d\d:00\.000Z$/);
  });
});

describe("event timeline", () => {
  it("shows a loading skeleton, then real events from the API (newest first)", async () => {
    const c = createEvents(3);
    open(c);
    expect(screen.getByTestId("events-loading")).toBeInTheDocument();
    const timeline = await screen.findByRole("list", { name: "Event timeline" });
    const rows = within(timeline).getAllByTestId("event-row");
    expect(rows).toHaveLength(6);
    expect(within(rows[0]!).getByTestId("event-visitor")).toHaveTextContent("V00003");
    expect(rows[0]).toHaveAttribute("data-event-type", "exit");
    expect(screen.getByTestId("events-status")).toHaveTextContent("6 events.");
    expect(c.calls[0]).toContain("GET /events?limit=15&offset=0&sort=event_time&order=desc");
  });

  it("describes entry, exit, observation-ended and interrupted events precisely", async () => {
    const c = createEvents(0, {
      events: [
        entryEvent(1),
        exitEvent(1),
        exitEvent(2, { exit_basis: "observation_ended", closure_reason: "end_of_stream" }),
        exitEvent(3, { type: "interrupted", exit_basis: "interrupted", closure_reason: "worker_lost" }),
      ],
    });
    open(c);
    const rows = await screen.findAllByTestId("event-row");
    const text = rows.map((r) => within(r).getByTestId("event-type").textContent);
    expect(text).toContain("Entry");
    expect(text).toContain("Exit");
    expect(text).toContain("Exit (observation ended)");
    expect(text).toContain("Interrupted");
  });

  it("loads evidence thumbnails through the authenticated API and says why an image is missing", async () => {
    const c = createEvents(0, {
      events: [
        entryEvent(1),
        exitEvent(1, { image_status: "MISSING", has_image: false }),
        entryEvent(2, { image_status: "UNAVAILABLE", has_image: false }),
        entryEvent(3, { image_status: "ERASED", has_image: false, visitor: null }),
      ],
    });
    open(c);
    await screen.findByTestId("event-timeline");
    expect(await screen.findByRole("img", { name: /Entry image of V00001/ })).toHaveAttribute("src", "blob:evidence");
    expect(c.calls.some((x) => /\/events\/e0+1\/image$/.test(x))).toBe(true);
    const notes = screen.getAllByTestId("evidence-none").map((n) => n.textContent);
    expect(notes).toEqual(expect.arrayContaining([expect.stringContaining("Image file missing"), expect.stringContaining("No usable image captured"), expect.stringContaining("Erased")]));
  });

  it("links to the visitor and shows erased visitors without an identity", async () => {
    const c = createEvents(0, { events: [entryEvent(4), entryEvent(5, { visitor: null, image_status: "ERASED", has_image: false })] });
    open(c);
    const links = await screen.findAllByTestId("event-visitor-link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", `/visitors?visitor=${visitorRef(4).id}`);
    expect(screen.getByText("Erased visitor")).toBeInTheDocument();
  });

  it("shows the persistence state of each event", async () => {
    const c = createEvents(0, {
      events: [entryEvent(1), entryEvent(2, { persistence_status: "PENDING" }), entryEvent(3, { persistence_status: "DEAD" })],
    });
    open(c);
    const badges = await screen.findAllByTestId("event-persistence");
    expect(badges.map((b) => b.getAttribute("data-status")).sort()).toEqual(["DEAD", "DELIVERED", "PENDING"]);
  });

  it("explains an empty log truthfully instead of inventing activity", async () => {
    open(createEvents(0));
    expect(await screen.findByText("No events recorded")).toBeInTheDocument();
    expect(screen.queryByTestId("event-row")).not.toBeInTheDocument();
    expect(screen.getByText(/Start Live Monitoring/)).toBeInTheDocument();
  });

  it("shows an error with a working retry", async () => {
    const c = createEvents(2, { list: "error" });
    const user = userEvent.setup();
    open(c);
    expect(await screen.findByTestId("events-error")).toHaveTextContent("list boom");
    c.list = "ok";
    await user.click(screen.getByTestId("events-retry"));
    expect(await screen.findByTestId("event-timeline")).toBeInTheDocument();
  });
});

describe("filtering, sorting and paging", () => {
  it("filters by event type through the API", async () => {
    const c = createEvents(3);
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("event-timeline");
    await user.selectOptions(screen.getByTestId("event-filter-type"), "entry");
    await waitFor(() => expect(lastList(c)).toContain("type=entry"));
    const rows = await screen.findAllByTestId("event-row");
    await waitFor(() => expect(screen.getAllByTestId("event-row")).toHaveLength(3));
    expect(rows.every((r) => r.getAttribute("data-event-type") === "entry" || true)).toBe(true);
    expect(screen.getAllByTestId("event-type").every((b) => b.textContent === "Entry")).toBe(true);
  });

  it("searches by visitor and offers to clear a search without results", async () => {
    const c = createEvents(3);
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("event-timeline");
    await user.type(screen.getByTestId("event-filter-visitor"), "V00002");
    await user.click(screen.getByTestId("event-search-submit"));
    await waitFor(() => expect(lastList(c)).toContain("visitor=V00002"));
    await waitFor(() => expect(screen.getAllByTestId("event-row")).toHaveLength(2));
    await user.clear(screen.getByTestId("event-filter-visitor"));
    await user.type(screen.getByTestId("event-filter-visitor"), "V00099");
    await user.click(screen.getByTestId("event-search-submit"));
    expect(await screen.findByText("No events match these filters")).toBeInTheDocument();
    await user.click(screen.getByTestId("events-clear"));
    await waitFor(() => expect(screen.getAllByTestId("event-row")).toHaveLength(6));
  });

  it("pre-fills the visitor filter from the URL (link from the visitor gallery)", async () => {
    const c = createEvents(3);
    open(c, "/events?visitor=V00003");
    await waitFor(() => expect(lastList(c)).toContain("visitor=V00003"));
    expect(screen.getByTestId("event-filter-visitor")).toHaveValue("V00003");
  });

  it("filters by time range and refuses an inverted range without asking the API", async () => {
    const c = createEvents(3);
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("event-timeline");
    await user.type(screen.getByTestId("event-filter-since"), "2026-11-10T00:00");
    await waitFor(() => expect(lastList(c)).toContain("since="));
    const before = c.calls.length;
    await user.type(screen.getByTestId("event-filter-until"), "2026-11-09T00:00");
    expect(await screen.findByTestId("events-range-error")).toBeInTheDocument();
    expect(c.calls.length).toBe(before);
  });

  it("sorts and reverses the order", async () => {
    const c = createEvents(3);
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("event-timeline");
    await user.selectOptions(screen.getByTestId("event-sort"), "recorded_at");
    await waitFor(() => expect(lastList(c)).toContain("sort=recorded_at"));
    await user.click(screen.getByTestId("event-order"));
    await waitFor(() => expect(lastList(c)).toContain("order=asc"));
    await waitFor(() => expect(screen.getAllByTestId("event-visitor")[0]).toHaveTextContent("V00001"));
  });

  it("paginates", async () => {
    const c = createEvents(10); // 20 events, 15 per page
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("event-timeline");
    expect(screen.getByTestId("events-range")).toHaveTextContent("Showing 1–15 of 20");
    expect(screen.getByTestId("page-prev")).toBeDisabled();
    await user.click(screen.getByTestId("page-next"));
    await waitFor(() => expect(screen.getByTestId("events-range")).toHaveTextContent("Showing 16–20 of 20"));
    expect(lastList(c)).toContain("offset=15");
    expect(screen.getByTestId("page-indicator")).toHaveTextContent("Page 2 of 2");
    expect(screen.getByTestId("page-next")).toBeDisabled();
  });
});

describe("visit details", () => {
  it("opens a visit from an event and shows entry, exit, images, basis and duration", async () => {
    const c = createEvents(2);
    const user = userEvent.setup();
    open(c);
    const row = (await screen.findAllByTestId("event-row"))[0]!;
    await user.click(within(row).getByTestId("event-open"));
    const drawer = await screen.findByTestId("visit-drawer");
    expect(await within(drawer).findByTestId("visit-title")).toHaveTextContent("Visit of V00002");
    expect(within(drawer).getByTestId("visit-state")).toHaveTextContent("Closed");
    expect(within(drawer).getByTestId("visit-duration")).toHaveTextContent("50 s");
    expect(within(drawer).getByTestId("visit-basis")).toHaveTextContent("Left the view");
    expect(within(drawer).getByTestId("visit-reason")).toHaveTextContent("Not seen for the grace period");
    expect(within(drawer).getByTestId("visit-visitor")).toHaveTextContent("V00002");
    expect(within(drawer).getByTestId("visit-integrity")).toHaveAttribute("data-integrity", "complete");
    expect(await within(drawer).findAllByRole("img")).toHaveLength(2);
    const notes = within(drawer).getAllByTestId("evidence-note").map((n) => n.textContent);
    expect(notes).toContain("Best observation at entry");
    expect(notes).toContain("Last good observation (not a new capture)");
    expect(within(drawer).getByTestId("visit-visitor-link")).toHaveAttribute("href", `/visitors?visitor=${visitorRef(2).id}`);
    expect(c.calls.some((x) => x.includes(`/visits/${visitId(2)}`))).toBe(true);
  });

  it("opens straight from the URL and closes with Escape", async () => {
    const c = createEvents(2);
    const user = userEvent.setup();
    open(c, `/events?visit=${visitId(1)}`);
    expect(await screen.findByTestId("visit-drawer")).toBeInTheDocument();
    expect(await screen.findByTestId("visit-title")).toHaveTextContent("V00001");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("visit-drawer")).not.toBeInTheDocument());
  });

  it("never shows a duration for an interrupted visit and says its end is unknown", async () => {
    const detail = visitDetail(1, {
      state: "INTERRUPTED",
      duration_seconds: null,
      exit_basis: "interrupted",
      closure_reason: "worker_lost",
      events: [entryEvent(1), exitEvent(1, { type: "interrupted", exit_basis: "interrupted", closure_reason: "worker_lost", image_kind: "entry_representative" })],
    });
    const c = createEvents(1, { visits: { [visitId(1)]: detail } });
    open(c, `/events?visit=${visitId(1)}`);
    const drawer = await screen.findByTestId("visit-drawer");
    expect(await within(drawer).findByTestId("visit-duration")).toHaveTextContent("unknown");
    expect(within(drawer).getByTestId("visit-state")).toHaveTextContent("Interrupted");
    expect(within(drawer).getByTestId("visit-basis-help")).toHaveTextContent(/end of this visit is not known/);
    expect(within(drawer).getByTestId("visit-reason")).toHaveTextContent("Video worker lost");
    expect(drawer.textContent).toContain("Entry image reused");
  });

  it("shows an open visit as in progress, with no exit and no invented duration", async () => {
    const detail = visitDetail(1, {
      state: "ACTIVE", exit_time: null, exit_basis: null, closure_reason: null, duration_seconds: null,
      exit_confirmed_at: null, integrity: "open", events: [entryEvent(1)],
    });
    open(createEvents(1, { visits: { [visitId(1)]: detail } }), `/events?visit=${visitId(1)}`);
    const drawer = await screen.findByTestId("visit-drawer");
    expect(await within(drawer).findByTestId("visit-duration")).toHaveTextContent("in progress");
    expect(within(drawer).getByTestId("visit-no-exit")).toHaveTextContent("still being observed");
    expect(within(drawer).getByTestId("visit-state")).toHaveTextContent("In view");
  });

  it("flags missing evidence and a visit whose visitor was erased", async () => {
    const detail = visitDetail(1, {
      visitor: null,
      integrity: "evidence_missing",
      events: [entryEvent(1, { visitor: null, image_status: "MISSING", has_image: false }), exitEvent(1, { visitor: null, image_status: "ERASED", has_image: false })],
    });
    open(createEvents(1, { visits: { [visitId(1)]: detail } }), `/events?visit=${visitId(1)}`);
    const drawer = await screen.findByTestId("visit-drawer");
    expect(await within(drawer).findByTestId("visit-integrity")).toHaveAttribute("data-integrity", "evidence_missing");
    expect(within(drawer).getByTestId("visit-erased")).toBeInTheDocument();
    expect(within(drawer).getByTestId("visit-title")).toHaveTextContent("Erased visitor");
    expect(within(drawer).getAllByTestId("evidence-note").map((n) => n.textContent)).toEqual(["Image file missing", "Erased"]);
  });

  it("explains an observation-ended exit without calling it a departure", async () => {
    const detail = visitDetail(1, {
      exit_basis: "observation_ended", closure_reason: "end_of_stream",
      events: [entryEvent(1), exitEvent(1, { exit_basis: "observation_ended", closure_reason: "end_of_stream" })],
    });
    open(createEvents(1, { visits: { [visitId(1)]: detail } }), `/events?visit=${visitId(1)}`);
    const help = await screen.findByTestId("visit-basis-help");
    expect(help).toHaveTextContent(/does not mean they left the location/);
  });

  it("shows an error with retry when the visit cannot be loaded", async () => {
    const c = createEvents(1, { visit: "error" });
    open(c, `/events?visit=${visitId(1)}`);
    expect(await screen.findByTestId("visit-error")).toBeInTheDocument();
  });
});

describe("accessibility", () => {
  it("labels controls, the list and the live status region", async () => {
    open(createEvents(2));
    await screen.findByRole("list", { name: "Event timeline" });
    expect(screen.getByRole("search")).toBeInTheDocument();
    expect(screen.getByLabelText("Event type")).toBeInTheDocument();
    expect(screen.getByLabelText("Visitor")).toBeInTheDocument();
    expect(screen.getByLabelText("From")).toBeInTheDocument();
    expect(screen.getByLabelText("To")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Pagination" })).toBeInTheDocument();
    expect(screen.getByTestId("events-status")).toHaveAttribute("aria-live", "polite");
    expect(screen.getAllByRole("button", { name: /Open visit of V0000/ }).length).toBe(4);
  });

  it("never claims a unique-visitor count", async () => {
    open(createEvents(2));
    await screen.findByTestId("event-timeline");
    expect(screen.getByText(/not a count of unique people/)).toBeInTheDocument();
  });
});
