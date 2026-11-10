import { render, screen } from "@testing-library/react";
import { MotionConfigContext } from "motion/react";
import { useContext } from "react";
import { describe, expect, it, vi } from "vitest";
import { Providers } from "@/app/providers";
import { api, ApiError } from "@/lib/api";
import { createQueryClient, shouldRetry } from "@/lib/query-client";
import { mockApi } from "./render";

describe("reduced motion", () => {
  it("Providers apply MotionConfig reducedMotion=user (transforms are skipped)", () => {
    function Probe() {
      const { reducedMotion } = useContext(MotionConfigContext);
      return <span data-testid="rm">{reducedMotion}</span>;
    }
    render(
      <Providers client={createQueryClient()}>
        <Probe />
      </Providers>,
    );
    expect(screen.getByTestId("rm")).toHaveTextContent("user");
  });

  it("globals.css disables transitions under prefers-reduced-motion", async () => {
    const { readFileSync } = await import("node:fs");
    const css = readFileSync("src/styles/globals.css", "utf-8");
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
    expect(css).toMatch(/transition-duration: 0\.01ms/);
  });
});

describe("api client", () => {
  it("maps connection failures to a network ApiError", async () => {
    mockApi({ health: "down" });
    await expect(api.health()).rejects.toMatchObject({ kind: "network" });
  });

  it("returns the readiness body on 503 instead of throwing", async () => {
    mockApi({ ready: "db-down" });
    const body = await api.readiness();
    expect(body.status).toBe("not_ready");
    expect(body.checks.database.status).toBe("unavailable");
  });

  it("parses the structured error envelope", async () => {
    mockApi({ status: "error" });
    const err = await api.systemStatus().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err).toMatchObject({ kind: "http", status: 500, code: "internal_error", requestId: "r1" });
  });

  it("handles non-JSON error bodies (e.g. proxy 502)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("Bad Gateway", { status: 502 })));
    await expect(api.config()).rejects.toMatchObject({ kind: "http", status: 502 });
  });
});

describe("retry policy", () => {
  it("retries network/5xx once but never 4xx", () => {
    expect(shouldRetry(0, new ApiError("x", "network"))).toBe(true);
    expect(shouldRetry(1, new ApiError("x", "network"))).toBe(false);
    expect(shouldRetry(0, new ApiError("x", "http", 503))).toBe(true);
    expect(shouldRetry(0, new ApiError("x", "http", 404))).toBe(false);
  });
});
