import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { formatRelative } from "@/features/visitors/format";
import { renderApp } from "./render";
import { createVisitors, visitorsFetch, type VisitorsController } from "./visitors-mocks";

beforeEach(() => {
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:face"), revokeObjectURL: vi.fn() }));
});

const lastList = (c: VisitorsController) => c.calls.filter((x) => x.startsWith("GET /visitors?")).at(-1) ?? "";

function open(c: VisitorsController, path = "/visitors") {
  return renderApp(path, {}, visitorsFetch(c));
}

describe("format helpers", () => {
  const now = Date.parse("2026-11-10T12:00:00Z");
  it("describes recent times relatively and old ones absolutely", () => {
    expect(formatRelative("2026-11-10T11:59:50Z", now)).toBe("just now");
    expect(formatRelative("2026-11-10T11:30:00Z", now)).toBe("30 min ago");
    expect(formatRelative("2026-11-10T09:00:00Z", now)).toBe("3 h ago");
    expect(formatRelative("2026-11-08T12:00:00Z", now)).toBe("2 d ago");
    expect(formatRelative("not a date", now)).toBe("—");
  });
});

describe("visitor gallery", () => {
  it("shows a loading skeleton, then real visitors from the API", async () => {
    const c = createVisitors(3);
    open(c);
    expect(screen.getByTestId("visitors-loading")).toBeInTheDocument();
    const grid = await screen.findByTestId("visitor-grid");
    expect(within(grid).getAllByRole("button", { name: /Open visitor V/ })).toHaveLength(3);
    expect(screen.getByTestId("visitor-card-2")).toHaveTextContent("V00002");
    expect(screen.getByTestId("visitors-status")).toHaveTextContent("3 visitors.");
    expect(c.calls[0]).toContain("GET /visitors?limit=12&offset=0&sort=last_seen&order=desc");
  });

  it("shows first and last seen with absolute times on hover", async () => {
    open(createVisitors(1));
    const card = await screen.findByTestId("visitor-card-1");
    expect(within(card).getByTestId("visitor-first")).toHaveAttribute("title");
    expect(within(card).getByTestId("visitor-last")).toHaveAttribute("title");
    expect(within(card).getByText("Registered")).toBeInTheDocument();
  });

  it("loads thumbnails through the authenticated API and falls back when an image is missing", async () => {
    const c = createVisitors(2);
    c.visitors[1]!.has_image = false;
    open(c);
    const grid = await screen.findByTestId("visitor-grid");
    const first = await within(screen.getByTestId("visitor-card-1")).findByRole("img", { name: /Face of visitor V00001/ });
    expect(first).toHaveAttribute("src", "blob:face");
    expect(within(screen.getByTestId("visitor-card-2")).getByTestId("image-none")).toBeInTheDocument();
    expect(c.calls.some((x) => x.includes("/visitors/") && x.endsWith("/image"))).toBe(true);
    expect(grid).toBeInTheDocument();
  });

  it("shows an 'image unavailable' placeholder when the image cannot be loaded", async () => {
    const c = createVisitors(1, { image: "missing" });
    open(c);
    expect(await screen.findByTestId("image-missing")).toHaveTextContent("Image unavailable");
  });

  it("shows an honest empty state when nobody is registered", async () => {
    open(createVisitors(0));
    expect(await screen.findByRole("heading", { level: 2, name: "No visitors registered yet" })).toBeInTheDocument();
    expect(screen.queryByTestId("visitor-grid")).not.toBeInTheDocument();
    expect(screen.queryByText(/V0000/)).not.toBeInTheDocument(); // no invented visitors
  });

  it("explains a failed load and retries", async () => {
    const user = userEvent.setup();
    const c = createVisitors(2, { list: "error" });
    open(c);
    expect(await screen.findByTestId("visitors-error")).toHaveTextContent("list boom");
    c.list = "ok";
    await user.click(screen.getByTestId("visitors-retry"));
    expect(await screen.findByTestId("visitor-grid")).toBeInTheDocument();
  });

  it("paginates and keeps the range honest", async () => {
    const user = userEvent.setup();
    const c = createVisitors(30);
    open(c);
    await screen.findByTestId("visitor-grid");
    expect(screen.getByTestId("visitors-range")).toHaveTextContent("Showing 1–12 of 30");
    expect(screen.getByTestId("page-indicator")).toHaveTextContent("Page 1 of 3");
    expect(screen.getByTestId("page-prev")).toBeDisabled();
    await user.click(screen.getByTestId("page-next"));
    await waitFor(() => expect(screen.getByTestId("visitors-range")).toHaveTextContent("Showing 13–24 of 30"));
    await user.click(screen.getByTestId("page-next"));
    await waitFor(() => expect(screen.getByTestId("visitors-range")).toHaveTextContent("Showing 25–30 of 30"));
    expect(screen.getByTestId("page-next")).toBeDisabled();
    expect(c.calls.some((x) => x.includes("offset=24"))).toBe(true);
  });

  it("searches by visitor id/number and offers to clear an empty result", async () => {
    const user = userEvent.setup();
    const c = createVisitors(20);
    open(c);
    await screen.findByTestId("visitor-grid");
    await user.type(screen.getByTestId("visitor-search"), "V00007");
    await user.click(screen.getByTestId("visitor-search-submit"));
    await waitFor(() => expect(screen.getByTestId("visitors-range")).toHaveTextContent("of 1"));
    expect(screen.getByTestId("visitor-card-7")).toBeInTheDocument();
    expect(lastList(c)).toContain("q=V00007");

    await user.clear(screen.getByTestId("visitor-search"));
    await user.type(screen.getByTestId("visitor-search"), "99999");
    await user.click(screen.getByTestId("visitor-search-submit"));
    expect(await screen.findByRole("heading", { name: "No visitors match your search" })).toBeInTheDocument();
    await user.click(screen.getByTestId("clear-search"));
    expect(await screen.findByTestId("visitor-grid")).toBeInTheDocument();
  });

  it("sorts by number and reverses the order", async () => {
    const user = userEvent.setup();
    const c = createVisitors(5);
    open(c);
    await screen.findByTestId("visitor-grid");
    await user.selectOptions(screen.getByTestId("visitor-sort"), "number");
    await waitFor(() => expect(lastList(c)).toContain("sort=number"));
    await user.click(screen.getByTestId("visitor-order"));
    await waitFor(() => expect(lastList(c)).toContain("order=asc"));
    await waitFor(() => {
      const labels = within(screen.getByTestId("visitor-grid")).getAllByTestId("visitor-label").map((e) => e.textContent);
      expect(labels).toEqual(["V00001", "V00002", "V00003", "V00004", "V00005"]);
    });
  });

  it("is accessible: labelled search, grid and buttons, polite status", async () => {
    open(createVisitors(2));
    await screen.findByTestId("visitor-grid");
    expect(screen.getByRole("search")).toBeInTheDocument();
    expect(screen.getByLabelText("Search by visitor ID")).toBeInTheDocument();
    expect(screen.getByLabelText("Sort by")).toBeInTheDocument();
    expect(screen.getByRole("list", { name: "Registered visitors" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Pagination" })).toBeInTheDocument();
    expect(screen.getByTestId("visitors-status")).toHaveAttribute("aria-live", "polite");
  });
});

describe("visitor details", () => {
  it("opens a drawer with real details and recognition history", async () => {
    const user = userEvent.setup();
    const c = createVisitors(3);
    const { router } = open(c);
    await screen.findByTestId("visitor-grid");
    await user.click(screen.getByTestId("visitor-card-2"));
    const drawer = await screen.findByTestId("visitor-drawer");
    expect(await within(drawer).findByTestId("drawer-title")).toHaveTextContent("V00002");
    expect(within(drawer).getByTestId("drawer-sessions")).toHaveTextContent("2");
    expect(within(drawer).getByTestId("drawer-first")).not.toHaveTextContent("—");
    expect(within(drawer).getByText("sface-2021dec")).toBeInTheDocument();
    const items = within(drawer).getAllByTestId("history-item");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("Track #7 (session-only) · registered here");
    expect(items[1]).toHaveTextContent("similarity 0.83");
    expect(items[1]).toHaveTextContent("1 conflicting observation");
    expect(within(drawer).getByText(/not a visit log/)).toBeInTheDocument();
    expect(router.state.location.search).toContain("visitor=");
    expect(drawer.textContent).not.toMatch(/embedding vector|unique visitors/i);
    // closing returns to the gallery
    await user.click(within(drawer).getByTestId("drawer-close"));
    await waitFor(() => expect(screen.queryByTestId("visitor-drawer")).not.toBeInTheDocument());
    expect(router.state.location.search).toBe("");
  });

  it("can be opened from a link and closes with Escape", async () => {
    const user = userEvent.setup();
    const c = createVisitors(2);
    open(c, `/visitors?visitor=${c.visitors[0]!.id}`);
    await waitFor(() => expect(screen.getByTestId("drawer-title")).toHaveTextContent("V00001"));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByTestId("visitor-drawer")).not.toBeInTheDocument());
  });

  it("shows an error with retry when the visitor cannot be loaded", async () => {
    const user = userEvent.setup();
    const c = createVisitors(1, { detail: "error" });
    open(c, `/visitors?visitor=${c.visitors[0]!.id}`);
    expect(await screen.findByTestId("drawer-error")).toHaveTextContent("could not be loaded");
    c.detail = "ok";
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("drawer-facts")).toBeInTheDocument();
  });

  it("erases a visitor only after an explicit confirmation", async () => {
    const user = userEvent.setup();
    const c = createVisitors(2);
    open(c);
    await screen.findByTestId("visitor-grid");
    await user.click(screen.getByTestId("visitor-card-1"));
    await user.click(await screen.findByTestId("drawer-erase"));
    expect(c.deleted).toEqual([]); // nothing happened yet
    expect(await screen.findByText(/permanently deletes the stored face image/)).toBeInTheDocument();
    await user.click(screen.getByTestId("erase-cancel"));
    expect(c.deleted).toEqual([]);
    await user.click(screen.getByTestId("drawer-erase"));
    await user.click(screen.getByTestId("erase-confirm"));
    await waitFor(() => expect(c.deleted).toHaveLength(1));
    await waitFor(() => expect(screen.queryByTestId("visitor-card-1")).not.toBeInTheDocument());
    expect(screen.getByTestId("visitor-card-2")).toBeInTheDocument();
  });
});
