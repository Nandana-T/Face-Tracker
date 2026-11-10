import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { useUiStore } from "@/lib/ui-store";
import { renderApp } from "./render";

describe("application shell & routing", () => {
  it("renders the shell with branding, navigation and skip link", async () => {
    renderApp("/");
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    for (const name of ["Overview", "Live Monitoring", "Visitors", "Event Logs", "Analytics", "Settings"]) {
      expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    }
    expect(screen.getAllByText("Katomaran AI").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Skip to content" })).toHaveAttribute("href", "#main");
  });

  it.each([
    ["Live Monitoring", "/live", "Live Monitoring"],
    ["Visitors", "/visitors", "Visitors"],
    ["Event Logs", "/events", "Event Logs"],
    ["Analytics", "/analytics", "Analytics"],
    ["Settings", "/settings", "Settings"],
  ])("navigates to %s and marks it active", async (name, path, heading) => {
    const user = userEvent.setup();
    const { router } = renderApp("/");
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    await user.click(within(nav).getByRole("link", { name }));
    expect(router.state.location.pathname).toBe(path);
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name })).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: "Overview" })).not.toHaveAttribute("aria-current");
    expect(screen.getByTestId("page-title")).toHaveTextContent(heading);
  });

  it("shows a not-found page for unknown routes", async () => {
    renderApp("/does-not-exist");
    expect(await screen.findByText("Page not found")).toBeInTheDocument();
  });

  it("collapses the sidebar, keeps links accessible by name and persists the state", async () => {
    const user = userEvent.setup();
    renderApp("/");
    const toggle = screen.getByRole("button", { name: "Collapse sidebar" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    await user.click(toggle);
    expect(useUiStore.getState().sidebarCollapsed).toBe(true);
    expect(screen.getByRole("button", { name: "Expand sidebar" })).toHaveAttribute("aria-expanded", "false");
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).getByRole("link", { name: "Visitors" })).toBeInTheDocument();
    expect(JSON.parse(localStorage.getItem("katomaran-ui") ?? "{}").state.sidebarCollapsed).toBe(true);
  });

  it("shows a tooltip for collapsed navigation items on keyboard focus", async () => {
    const user = userEvent.setup();
    useUiStore.setState({ sidebarCollapsed: true });
    renderApp("/");
    await user.tab(); // skip link
    await user.tab(); // first nav link
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(within(nav).getByRole("link", { name: "Overview" })).toHaveFocus();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Overview");
  });

  it("opens the mobile navigation drawer and navigates", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/");
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const mobile = await screen.findByRole("navigation", { name: "Mobile navigation" });
    await user.click(within(mobile).getByRole("link", { name: "Analytics" }));
    expect(router.state.location.pathname).toBe("/analytics");
    expect(screen.queryByRole("navigation", { name: "Mobile navigation" })).not.toBeInTheDocument();
  });
});

describe("theme", () => {
  it("toggles between dark and light and updates the document class", async () => {
    const user = userEvent.setup();
    renderApp("/");
    expect(document.documentElement).toHaveClass("dark");
    await user.click(screen.getByRole("button", { name: "Switch to light theme" }));
    expect(document.documentElement).not.toHaveClass("dark");
    await user.click(screen.getByRole("button", { name: "Switch to dark theme" }));
    expect(document.documentElement).toHaveClass("dark");
  });
});
