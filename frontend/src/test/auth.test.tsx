import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamUrl } from "@/lib/api";
import { authToken } from "@/lib/auth";
import {
  FakeSocket,
  apiError,
  createController,
  installFakeStreaming,
  processingFetch,
  sessionFixture,
} from "./monitoring-mocks";
import { renderApp } from "./render";

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const pathOf = (input: RequestInfo | URL) =>
  new URL(String(input), "http://localhost").pathname.replace(/^.*\/v1/, "");

/** A server that requires sign-in: everything except health/ready/auth answers 401 without the token. */
function securedFetch(password = "right-password") {
  const seen: { path: string; auth: string | null }[] = [];
  const wrap = (base: typeof fetch): typeof fetch => {
    const processing = processingFetch(createController())(base);
    return (async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = pathOf(input);
      const auth = new Headers(init?.headers).get("Authorization");
      seen.push({ path, auth });
      if (path === "/auth/status") {
        return json({ required: true, authenticated: auth === "Bearer good-token" });
      }
      if (path === "/auth/login") {
        const body = JSON.parse(String(init?.body));
        return body.password === password
          ? json({ access_token: "good-token", token_type: "bearer", expires_in: 28800 })
          : apiError(401, "invalid_credentials", "Incorrect password.");
      }
      if (path === "/health" || path === "/ready") return base(input, init);
      if (auth !== "Bearer good-token") return apiError(401, "unauthorized", "Sign in to use this API.");
      return processing(input, init);
    }) as typeof fetch;
  };
  return { wrap, seen };
}

beforeEach(() => {
  authToken.clear();
  installFakeStreaming();
});
afterEach(() => authToken.clear());

describe("sign-in gate", () => {
  it("shows the sign-in form instead of the app when the API requires it", async () => {
    const { wrap } = securedFetch();
    renderApp("/live", {}, wrap);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("viewport")).not.toBeInTheDocument();
  });

  it("signs in with the right password, stores a token and sends it on later calls", async () => {
    const { wrap, seen } = securedFetch();
    renderApp("/live", {}, wrap);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Password"), "right-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("viewport")).toBeInTheDocument();
    expect(authToken.get()).toBe("good-token");
    await waitFor(() =>
      expect(seen.some((s) => s.path === "/sources" && s.auth === "Bearer good-token")).toBe(true),
    );
    expect(await screen.findByTestId("sign-out")).toBeInTheDocument();
  });

  it("explains a wrong password without storing anything", async () => {
    const { wrap } = securedFetch();
    renderApp("/live", {}, wrap);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Password"), "nope");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("That password is not correct.");
    expect(authToken.get()).toBeNull();
    expect(screen.getByLabelText("Password")).toHaveAttribute("aria-invalid", "true");
  });

  it("reports throttling distinctly", async () => {
    const wrap = (base: typeof fetch): typeof fetch =>
      (async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = pathOf(input);
        if (path === "/auth/status") return json({ required: true, authenticated: false });
        if (path === "/auth/login") return apiError(429, "too_many_attempts", "Too many.");
        return base(input, init);
      }) as typeof fetch;
    renderApp("/", {}, wrap);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Password"), "x");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Too many failed attempts/);
  });

  it("signs out: the token is dropped and the form returns", async () => {
    const { wrap } = securedFetch();
    renderApp("/live", {}, wrap);
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Password"), "right-password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await user.click(await screen.findByTestId("sign-out"));
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(authToken.get()).toBeNull();
  });

  it("returns to the form when the server rejects the stored token", async () => {
    authToken.set("expired-token");
    let calls = 0;
    const wrap = (base: typeof fetch): typeof fetch => {
      const processing = processingFetch(createController())(base);
      return (async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = pathOf(input);
        const auth = new Headers(init?.headers).get("Authorization");
        if (path === "/auth/status") {
          calls += 1;
          // First look at the stored token: it is accepted. After a 401 elsewhere it is not.
          return json({ required: true, authenticated: calls === 1 && auth === "Bearer expired-token" });
        }
        if (path === "/sources") {
          // The token expires server-side just after the status check succeeded.
          await new Promise((r) => setTimeout(r, 150));
          return apiError(401, "unauthorized", "Sign in to use this API.");
        }
        return processing(input, init);
      }) as typeof fetch;
    };
    renderApp("/live", {}, wrap);
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
    expect(authToken.get()).toBeNull();
  });

  it("does not show the form when the server does not require sign-in", async () => {
    renderApp("/live", {}, processingFetch(createController()));
    expect(await screen.findByTestId("viewport")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sign in" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("sign-out")).not.toBeInTheDocument();
  });
});

describe("stream tickets", () => {
  it("asks for a fresh single-use ticket for every connection attempt", async () => {
    const c = createController({
      active: sessionFixture({ state: "RUNNING" }),
      current: sessionFixture({ state: "RUNNING" }),
    });
    renderApp("/live", {}, processingFetch(c));
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));
    expect(FakeSocket.last.url).toContain("?ticket=ticket-1");
    FakeSocket.last.serverClose(); // connection lost -> reconnect with back-off (500 ms)
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(2), { timeout: 3000 });
    expect(FakeSocket.last.url).toContain("?ticket=ticket-2");
    expect(c.tickets).toBe(2);
  });

  it("never puts the access token in a URL", () => {
    authToken.set("secret-access-token");
    expect(streamUrl("abc", "t1")).not.toContain("secret-access-token");
    expect(streamUrl("abc", "t1")).toMatch(/\/sessions\/abc\/stream\?ticket=t1$/);
    expect(streamUrl("abc")).not.toContain("?");
  });
});

vi.setConfig({ testTimeout: 15_000 });
