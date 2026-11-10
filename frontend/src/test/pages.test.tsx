import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { useUiStore } from "@/lib/ui-store";
import { renderApp } from "./render";

describe("overview empty states (no invented data)", () => {
  it("shows placeholders instead of numbers", async () => {
    renderApp("/");
    const values = await screen.findAllByTestId("metric-value");
    expect(values).toHaveLength(4);
    for (const v of values) expect(v).toHaveTextContent("—");
    expect(screen.getAllByText("No video source connected.").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Visitor analytics will appear after processing begins.").length).toBeGreaterThan(0);
  });

  it("derives onboarding progress from real state", async () => {
    renderApp("/");
    await screen.findByTestId("system-status");
    await waitFor(() => expect(screen.getAllByLabelText("Done")).toHaveLength(2));
    expect(screen.getByLabelText("Planned")).toBeInTheDocument();
  });
});

describe.each([
  ["/visitors", "Visitors", "No visitors registered yet"],
  ["/events", "Event Logs", "No events recorded"],
  ["/analytics", "Analytics", "No analytics available"],
])("%s placeholder page", (path, heading, empty) => {
  it("renders heading and empty state", async () => {
    renderApp(path);
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: empty })).toBeInTheDocument();
  });
});

describe("/live page shell", () => {
  it("renders the monitoring console instead of a placeholder", async () => {
    renderApp("/live");
    expect(await screen.findByRole("heading", { level: 1, name: "Live Monitoring" })).toBeInTheDocument();
    expect(await screen.findByTestId("viewport")).toBeInTheDocument();
    expect(screen.getByText(/Face detection and temporary tracking/)).toBeInTheDocument();
  });
});

describe("settings", () => {
  it("shows the real backend configuration read-only", async () => {
    renderApp("/settings");
    const sections = await screen.findByTestId("config-sections");
    expect(within(sections).getByText("Frame skip")).toBeInTheDocument();
    expect(within(sections).getByText("0.55")).toBeInTheDocument();
    expect(within(sections).getByText("Not configured")).toBeInTheDocument();
  });

  it("shows an error state when configuration cannot be loaded", async () => {
    renderApp("/settings", { config: "error" });
    expect(await screen.findByText("Configuration unavailable")).toBeInTheDocument();
    expect(screen.getByText("config boom")).toBeInTheDocument();
  });

  it("resets preferences only after confirmation", async () => {
    const user = userEvent.setup();
    useUiStore.setState({ theme: "light", sidebarCollapsed: true });
    renderApp("/settings");
    await user.click(await screen.findByRole("button", { name: /reset interface preferences/i }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(useUiStore.getState().theme).toBe("light");

    await user.click(screen.getByRole("button", { name: /reset interface preferences/i }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Reset" }));
    expect(useUiStore.getState()).toMatchObject({ theme: "dark", sidebarCollapsed: false });
    expect(await screen.findByText("Interface preferences reset")).toBeInTheDocument();
  });
});
