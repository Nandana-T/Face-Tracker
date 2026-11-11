import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { renderApp } from "./render";

const indicators = () => screen.getAllByTestId("connection-indicators")[0]!;

describe("backend connection indicators", () => {
  it("shows checking, then connected for API and database", async () => {
    renderApp("/");
    expect(within(indicators()).getByRole("status", { name: "API: Checking…" })).toBeInTheDocument();
    expect(await within(indicators()).findByRole("status", { name: "API: Connected" })).toBeInTheDocument();
    expect(await within(indicators()).findByRole("status", { name: "Database: Connected" })).toBeInTheDocument();
  });

  it("distinguishes an unavailable database from an unreachable API", async () => {
    renderApp("/", { ready: "db-down" });
    expect(await within(indicators()).findByRole("status", { name: "API: Connected" })).toBeInTheDocument();
    expect(await within(indicators()).findByRole("status", { name: "Database: Unavailable" })).toBeInTheDocument();
  });

  it("shows a disconnected state when the backend is unreachable", async () => {
    renderApp("/", { health: "down" });
    expect(await within(indicators()).findByRole("status", { name: "API: Disconnected" })).toBeInTheDocument();
    expect(within(indicators()).getByRole("status", { name: "Database: Unknown" })).toBeInTheDocument();
    expect(await screen.findByText("Backend not reachable")).toBeInTheDocument();
  });

  it("keeps the app usable and recovers after the backend comes back", async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderApp("/", { health: "down" });
    await screen.findByText("Backend not reachable");
    // Navigation still works while disconnected.
    await user.click(screen.getByRole("link", { name: "Visitors" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Visitors" })).toBeInTheDocument();
    await user.click(screen.getByRole("link", { name: "Overview" }));

    // Backend returns: swap the mock and press "Check again".
    const { mockApi } = await import("./render");
    fetchMock.mockRestore();
    mockApi({});
    await user.click(await screen.findByRole("button", { name: /check again/i }));
    await waitFor(() =>
      expect(within(indicators()).getByRole("status", { name: "API: Connected" })).toBeInTheDocument(),
    );
    expect(await screen.findByTestId("system-status")).toBeInTheDocument();
  });
});

describe("overview system status", () => {
  it("renders real status values reported by the backend", async () => {
    renderApp("/");
    const panel = await screen.findByTestId("system-status");
    expect(within(panel).getByText("katomaran-api v0.1.0 (test)")).toBeInTheDocument();
    expect(within(panel).getByText("2m 5s")).toBeInTheDocument();
    expect(within(panel).getByText("Applied (revision 0001)")).toBeInTheDocument();
    // Worker, detector, tracker and recognizer all run in the worker, which is not connected here.
    expect(within(panel).getAllByText("Not running")).toHaveLength(4);
    expect(within(panel).queryByText("Not implemented yet")).not.toBeInTheDocument();
  });

  it("shows loading skeletons while status is pending", async () => {
    renderApp("/", { status: "pending" });
    await screen.findAllByRole("status", { name: "API: Connected" });
    expect(screen.getAllByRole("status", { name: "Loading" }).length).toBeGreaterThan(0);
  });

  it("shows an error state with retry when status fails", async () => {
    renderApp("/", { status: "error" });
    expect(await screen.findByText("Could not load system status")).toBeInTheDocument();
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
