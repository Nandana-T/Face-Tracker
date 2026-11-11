import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { bucketTick } from "@/features/analytics/analytics-page";
import { analyticsStreamUrl } from "@/lib/api";
import {
  analyticsFetch,
  coverageFixture,
  createAnalytics,
  overviewFixture,
  pointFixture,
  reconcileFixture,
  timelineFixture,
  type AnalyticsController,
} from "./analytics-mocks";
import { FakeSocket, installFakeStreaming } from "./monitoring-mocks";
import { renderApp } from "./render";

const open = (c: AnalyticsController, path = "/analytics") => renderApp(path, {}, analyticsFetch(c));
const metric = (name: string) => screen.getByText((_, el) => el?.getAttribute("data-metric") === name);
const value = (name: string) => within(metric(name)).getByTestId("analytics-value");
const calls = (c: AnalyticsController, kind: string) => c.calls.filter((x) => x.startsWith(`GET /analytics/${kind}`));

beforeEach(() => {
  installFakeStreaming();
});
afterEach(() => vi.restoreAllMocks());

describe("helpers", () => {
  it("builds the stream URL from the API base and keeps the ticket out of the path", () => {
    expect(analyticsStreamUrl("a b")).toMatch(/\/analytics\/stream\?ticket=a%20b$/);
    expect(analyticsStreamUrl()).toMatch(/\/analytics\/stream$/);
  });
  it("formats day buckets as UTC dates", () => {
    expect(bucketTick("2026-11-10T00:00:00Z", "day")).toMatch(/Nov/);
  });
});

describe("analytics page - real numbers and honest labels", () => {
  it("shows a skeleton, then every figure from the API with its scope", async () => {
    const c = createAnalytics();
    open(c);
    expect(screen.getAllByTestId("metric-skeleton").length).toBeGreaterThan(0);
    await waitFor(() => expect(value("unique_visitors")).toHaveTextContent("4"));
    expect(value("registered_visitors")).toHaveTextContent("5");
    expect(value("returning_visitors")).toHaveTextContent("1");
    expect(value("visits")).toHaveTextContent("7");
    expect(value("entries")).toHaveTextContent("7");
    expect(value("exits")).toHaveTextContent("5"); // 3 + 2: interrupted visits are not exits
    expect(value("interrupted")).toHaveTextContent("1");
    expect(value("active_visits")).toHaveTextContent("1");
    // each number says whether it follows the filters
    expect(metric("registered_visitors")).toHaveAttribute("data-scope", "global");
    expect(metric("unique_visitors")).toHaveAttribute("data-scope", "filtered");
    expect(metric("active_visits")).toHaveAttribute("data-scope", "live");
    expect(within(metric("exits")).getByText(/3 left the view · 2 observation ended/)).toBeInTheDocument();
    expect(screen.getByTestId("updated-at")).toHaveTextContent("Updated");
  });

  it("states what unique visitors means and that it is not a count of people", async () => {
    open(createAnalytics());
    const note = await screen.findByTestId("counting-note");
    expect(note).toHaveTextContent("distinct registered face identities");
    expect(note).toHaveTextContent("not a count of physical people");
  });

  it("shows the recognition-coverage diagnostic with its disclaimer and never adds it to visitors", async () => {
    open(createAnalytics());
    const panel = await screen.findByTestId("coverage-panel");
    await within(panel).findByTestId("cov-share");
    expect(within(panel).getByTestId("cov-confirmed_tracks")).toHaveTextContent("74");
    expect(within(panel).getByTestId("cov-tracks_unresolved")).toHaveTextContent("11");
    expect(within(panel).getByTestId("cov-share")).toHaveTextContent("45%");
    expect(within(panel).getByTestId("cov-share")).toHaveTextContent("operational diagnostic");
    expect(within(panel).getByTestId("cov-disclaimer")).toHaveTextContent("not person-level recall");
    expect(within(panel).getByTestId("cov-rejected")).toHaveTextContent("140");
    await waitFor(() => expect(value("unique_visitors")).toHaveTextContent("4")); // 11 unresolved tracks add nobody
  });

  it("explains an empty database instead of inventing activity", async () => {
    const c = createAnalytics({ overview: overviewFixture({ registered_visitors: 0, unique_visitors_in_scope: 0, visits: 0, entry_events: 0, terminal_events: 0, terminal_by_basis: { absence: 0, observation_ended: 0, interrupted: 0 }, active_visits: 0 }), timeline: timelineFixture([]) });
    open(c);
    const empty = await screen.findByTestId("analytics-empty");
    expect(empty).toHaveTextContent("No analytics available");
    expect(screen.queryByTestId("chart-activity")).not.toBeInTheDocument();
    await waitFor(() => expect(value("visits")).toHaveTextContent("0"));
  });
});

describe("charts", () => {
  it("draws the returned points exactly and offers the same data as a table", async () => {
    open(createAnalytics());
    const chart = await screen.findByTestId("chart-activity");
    expect(within(chart).getByRole("img", { name: "Unique visitors per period" })).toBeInTheDocument();
    expect(within(chart).getAllByTestId("bar-unique_visitors")).toHaveLength(3);
    const rows = within(screen.getByTestId("chart-activity-table")).getAllByRole("row");
    expect(rows).toHaveLength(4); // header + 3 buckets
    expect(rows[2]).toHaveTextContent("2026-11-10T10:00:00Z");
    expect(rows[2]).toHaveTextContent("2"); // that bucket's unique visitors
  });

  it("switches the activity chart between unique visitors and visits", async () => {
    const user = userEvent.setup();
    open(createAnalytics());
    await screen.findByTestId("chart-activity");
    expect(screen.getByTestId("metric-unique_visitors")).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByTestId("metric-visits"));
    const chart = screen.getByTestId("chart-activity");
    expect(within(chart).getByRole("img", { name: "Visits per period" })).toBeInTheDocument();
    expect(within(chart).getAllByTestId("bar-visits").length).toBe(3);
    expect(screen.getByTestId("metric-visits")).toHaveAttribute("aria-pressed", "true");
  });

  it("keeps terminal-event kinds distinguishable in the entry/exit chart", async () => {
    open(createAnalytics({ timeline: timelineFixture([pointFixture(9, { entries: 3, exits_absence: 1, exits_observation_ended: 1, interrupted: 1 })]) }));
    const flow = await screen.findByTestId("chart-flow");
    for (const key of ["entries", "left", "ended", "interrupted"]) expect(within(flow).getByTestId(`bar-${key}`)).toBeInTheDocument();
    expect(flow).toHaveTextContent("Left the view");
    expect(flow).toHaveTextContent("Observation ended");
    expect(flow).toHaveTextContent("Interrupted");
  });

  it("shows composition of new and returning identities", async () => {
    open(createAnalytics());
    const comp = await screen.findByTestId("composition");
    expect(within(comp).getByTestId("part-new")).toHaveTextContent("3");
    expect(within(comp).getByTestId("part-returning")).toHaveTextContent("1");
    expect(screen.getByText(/does not count/)).toBeInTheDocument();
  });

  it("lists sources and sessions from the database", async () => {
    open(createAnalytics());
    expect(await screen.findAllByTestId("source-row")).toHaveLength(2);
    const sessions = await screen.findAllByTestId("session-row");
    expect(sessions[0]).toHaveTextContent("Hall");
    expect(sessions[0]).toHaveTextContent("live");
    expect(sessions[0]).toHaveTextContent("1 interrupted");
  });

  it("lists recent events with links to the visit", async () => {
    open(createAnalytics());
    const items = await screen.findAllByTestId("recent-item");
    expect(items).toHaveLength(2);
    expect(within(items[0]!).getByRole("link")).toHaveAttribute("href", expect.stringContaining("/events?visit="));
  });
});

describe("filters apply to every figure, chart and table", () => {
  it("period presets, source and session change every request", async () => {
    const c = createAnalytics();
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("chart-activity");
    expect(calls(c, "overview")[0]).toBe("GET /analytics/overview");
    await user.click(screen.getByTestId("preset-24h"));
    await waitFor(() => expect(calls(c, "overview").at(-1)).toContain("since="));
    for (const kind of ["timeline", "sources", "sessions", "coverage", "recent"]) {
      await waitFor(() => expect(calls(c, kind).at(-1)).toContain("since="));
    }
    expect(calls(c, "timeline").at(-1)).toContain("bucket=hour"); // a 24 h window is charted hourly
    await user.selectOptions(screen.getByTestId("analytics-source"), "");
    await user.click(screen.getByTestId("preset-all"));
    await waitFor(() => expect(calls(c, "timeline").at(-1)).toContain("bucket=day"));
    expect(calls(c, "overview").at(-1)).toBe("GET /analytics/overview");
  });

  it("filters by session and source and offers a reset", async () => {
    const c = createAnalytics();
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("chart-activity");
    await waitFor(() => expect(within(screen.getByTestId("analytics-session")).getAllByRole("option").length).toBeGreaterThan(1));
    await user.selectOptions(screen.getByTestId("analytics-session"), "a".repeat(32));
    await waitFor(() => expect(calls(c, "overview").at(-1)).toContain(`session_id=${"a".repeat(32)}`));
    await user.click(screen.getByTestId("analytics-reset"));
    await waitFor(() => expect(calls(c, "overview").at(-1)).toBe("GET /analytics/overview"));
    expect(screen.queryByTestId("analytics-reset")).not.toBeInTheDocument();
  });

  it("explains a filter without results and refuses an inverted range without asking the API", async () => {
    const c = createAnalytics();
    const user = userEvent.setup();
    open(c);
    await screen.findByTestId("chart-activity");
    c.overview = overviewFixture({ visits: 0, unique_visitors_in_scope: 0, entry_events: 0, terminal_events: 0 });
    await user.click(screen.getByTestId("preset-custom"));
    await user.type(screen.getByTestId("analytics-since"), "2026-11-10T00:00");
    expect(await screen.findByText("No visits match these filters")).toBeInTheDocument();
    await user.type(screen.getByTestId("analytics-until"), "2026-11-09T00:00");
    expect(await screen.findByTestId("analytics-range-error")).toBeInTheDocument();
    const settled = c.calls.length; // an inverted range sends nothing further
    await new Promise((r) => setTimeout(r, 150));
    expect(c.calls.length).toBe(settled);
    expect(screen.queryByTestId("analytics-metric")).not.toBeInTheDocument();
  });
});

describe("failures and recovery", () => {
  it("shows no numbers when the backend fails and recovers on retry", async () => {
    const c = createAnalytics({ fail: { overview: true } });
    const user = userEvent.setup();
    open(c);
    expect(await screen.findByTestId("analytics-error")).toHaveTextContent("overview boom");
    expect(screen.queryByTestId("analytics-metric")).not.toBeInTheDocument(); // no stale or invented numbers
    c.fail.overview = false;
    await user.click(screen.getByTestId("analytics-retry"));
    await waitFor(() => expect(value("visits")).toHaveTextContent("7"));
  });

  it("isolates a failing chart and a failing coverage panel from the rest", async () => {
    const c = createAnalytics({ fail: { timeline: true, coverage: true } });
    open(c);
    await waitFor(() => expect(value("visits")).toHaveTextContent("7"));
    expect((await screen.findAllByTestId("chart-error")).length).toBe(2);
    expect(await screen.findByTestId("coverage-error")).toBeInTheDocument();
    expect(screen.getAllByTestId("source-row")).toHaveLength(2);
  });

  it("is honest about stale open visits (not present) and erased visitors", async () => {
    open(createAnalytics({ overview: overviewFixture({ active_visits: 0, stale_open_visits: 2, visits_without_identity: 1 }) }));
    await waitFor(() => expect(value("active_visits")).toHaveTextContent("0"));
    expect(within(metric("active_visits")).getByText(/2 stale open \(not counted\)/)).toBeInTheDocument();
    expect(screen.getByTestId("updated-at")).toHaveTextContent("erased visitors");
  });
});

describe("live updates", () => {
  it("refetches from the database when the stream announces a change - the notice carries no numbers", async () => {
    const c = createAnalytics();
    open(c);
    await waitFor(() => expect(FakeSocket.instances.length).toBe(1));
    expect(FakeSocket.last.url).toContain("/analytics/stream?ticket=t1");
    await act(async () => FakeSocket.last.open());
    await waitFor(() => expect(screen.getByTestId("live-indicator")).toHaveAttribute("data-state", "live"));
    await waitFor(() => expect(value("visits")).toHaveTextContent("7"));
    c.overview = overviewFixture({ visits: 8, entry_events: 8 }); // the database changed
    const before = calls(c, "overview").length;
    await act(async () => FakeSocket.last.message(JSON.stringify({ type: "analytics_changed", reason: "visit_event" })));
    await waitFor(() => expect(value("visits")).toHaveTextContent("8"));
    expect(calls(c, "overview").length).toBeGreaterThan(before);
  });

  it("ignores malformed notices, shows when live updates are off and reconnects", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const c = createAnalytics();
      open(c);
      await vi.waitFor(() => expect(FakeSocket.instances.length).toBe(1));
      await act(async () => FakeSocket.last.open());
      await act(async () => FakeSocket.last.message("not json"));
      await act(async () => FakeSocket.last.serverClose());
      await vi.waitFor(() => expect(screen.getByTestId("live-indicator")).toHaveAttribute("data-state", "offline"));
      expect(screen.getByTestId("live-indicator")).toHaveTextContent("refreshing every 20 s");
      await act(async () => vi.advanceTimersByTimeAsync(1100));
      await vi.waitFor(() => expect(FakeSocket.instances.length).toBe(2)); // a new socket with a fresh ticket
      expect(calls(c, "overview").length).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps working without live updates when no ticket can be obtained", async () => {
    const c = createAnalytics({ fail: { ticket: true } });
    open(c);
    await waitFor(() => expect(value("visits")).toHaveTextContent("7"));
    await waitFor(() => expect(screen.getByTestId("live-indicator")).toHaveAttribute("data-state", "offline"));
    expect(FakeSocket.instances.length).toBe(0);
  });
});

describe("consistency check", () => {
  it("runs on demand, reports differences and says it is read-only", async () => {
    const c = createAnalytics({
      reconcile: reconcileFixture({ consistent: false, errors: 1, checks: [{ name: "visits", ok: false, expected: 3, actual: 2, detail: "COUNT(visits) vs rows", severity: "error" }] }),
    });
    const user = userEvent.setup();
    open(c);
    const panel = await screen.findByTestId("consistency-panel");
    expect(panel).toHaveTextContent(/nothing is ever modified/i);
    expect(calls(c, "reconciliation")).toHaveLength(0); // never automatic
    await user.click(screen.getByTestId("consistency-run"));
    const result = await screen.findByTestId("consistency-result");
    expect(result).toHaveAttribute("data-consistent", "false");
    expect(within(result).getByTestId("consistency-issue")).toHaveTextContent("expected 3, found 2");
  });

  it("reports agreement", async () => {
    const user = userEvent.setup();
    open(createAnalytics());
    await user.click(await screen.findByTestId("consistency-run"));
    expect(await screen.findByText("All counts agree with the underlying records.")).toBeInTheDocument();
  });
});

describe("keyboard and accessibility", () => {
  it("exposes presets as a labelled group with pressed state and is operable with the keyboard", async () => {
    const user = userEvent.setup();
    open(createAnalytics());
    await screen.findByTestId("chart-activity");
    const group = screen.getByRole("group", { name: "Period presets" });
    expect(within(group).getByTestId("preset-all")).toHaveAttribute("aria-pressed", "true");
    within(group).getByTestId("preset-7d").focus();
    await user.keyboard("{Enter}");
    expect(within(group).getByTestId("preset-7d")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Source")).toBeInTheDocument();
    expect(screen.getByLabelText("Session")).toBeInTheDocument();
    expect(screen.getByLabelText("Chart interval")).toBeInTheDocument();
    expect(screen.getByTestId("live-indicator")).toHaveAttribute("aria-live", "polite");
  });

  it("never claims a number of physical people", async () => {
    open(createAnalytics());
    await screen.findByTestId("chart-activity");
    expect(document.body.textContent).not.toMatch(/total (people|persons)|number of people|physical visitors:/i);
    expect(coverageFixture().disclaimer).toMatch(/tracks are not people/);
  });
});

describe("overview page", () => {
  it("shows real figures, separates visits from identities and says what is a diagnostic", async () => {
    open(createAnalytics(), "/");
    await waitFor(() => expect(screen.getAllByTestId("metric-value")[0]).toHaveTextContent("5"));
    const cards = screen.getAllByTestId("metric-value").map((n) => n.textContent);
    expect(cards).toEqual(["5", "1", "7", "5"]); // identities, present now, entries, exits(3+2)
    const more = await screen.findByTestId("overview-more");
    expect(within(more).getAllByTestId("analytics-value").map((n) => n.textContent)).toEqual(["7", "1", "9"]);
    expect(within(more).getByText(/not extra people/)).toBeInTheDocument();
    expect(await screen.findByTestId("overview-recent")).toBeInTheDocument();
  });

  it("shows dashes and an honest notice when analytics cannot be loaded", async () => {
    open(createAnalytics({ fail: { overview: true, recent: true } }), "/");
    expect(await screen.findByTestId("overview-analytics-error")).toBeInTheDocument();
    for (const v of screen.getAllByTestId("metric-value")) expect(v).toHaveTextContent("—");
    expect(screen.getAllByText("Visitor analytics will appear after processing begins.").length).toBeGreaterThan(0);
  });
});
