import { describe, expect, it } from "vitest";
import { describeView, type ViewKey } from "@/features/monitoring/status-copy";
import { everyNth } from "@/features/monitoring/detection-summary";
import { sessionFixture, sourcesFixture } from "./monitoring-mocks";

const base = { session: null, sources: sourcesFixture(), sourcesFailed: false, startErrorCode: null };

describe("describeView", () => {
  it.each<[string, Parameters<typeof describeView>[0], ViewKey]>([
    ["backend unreachable", { ...base, sources: undefined, sourcesFailed: true }, "api_unreachable"],
    [
      "no source available",
      { ...base, sources: sourcesFixture({ sources: [] }) },
      "no_source",
    ],
    [
      "only unavailable sources",
      {
        ...base,
        sources: sourcesFixture({
          sources: [{ id: "x", label: "X", type: "file", availability: "unavailable", detail: "gone" }],
        }),
      },
      "no_source",
    ],
    [
      "worker offline",
      { ...base, sources: sourcesFixture({ worker: { connected: false, worker_id: null, model_available: null, model_detail: null } }) },
      "worker_offline",
    ],
    [
      "model unavailable",
      { ...base, sources: sourcesFixture({ worker: { connected: true, worker_id: "w", model_available: false, model_detail: "x" } }) },
      "model_unavailable",
    ],
    ["ready", base, "idle"],
    ["starting", { ...base, session: sessionFixture({ state: "STARTING" }) }, "connecting"],
    ["running", { ...base, session: sessionFixture({ state: "RUNNING" }) }, "processing"],
    [
      "running but camera reconnecting",
      { ...base, session: sessionFixture({ state: "RUNNING", connection: "reconnecting" }) },
      "camera_disconnected",
    ],
    ["stopping", { ...base, session: sessionFixture({ state: "STOPPING" }) }, "stopping"],
    ["completed", { ...base, session: sessionFixture({ state: "COMPLETED" }) }, "completed"],
    ["stopped", { ...base, session: sessionFixture({ state: "STOPPED" }) }, "stopped"],
    [
      "camera error",
      { ...base, session: sessionFixture({ state: "ERROR", error: { code: "camera_unavailable", message: "m" } }) },
      "camera_disconnected",
    ],
    [
      "model error from the worker",
      { ...base, session: sessionFixture({ state: "ERROR", error: { code: "model_unavailable", message: "m" } }) },
      "model_unavailable",
    ],
    [
      "worker lost",
      { ...base, session: sessionFixture({ state: "ERROR", error: { code: "worker_lost", message: "m" } }) },
      "worker_offline",
    ],
    [
      "unreadable video",
      { ...base, session: sessionFixture({ state: "ERROR", error: { code: "source_invalid", message: "Not a video." } }) },
      "source_problem",
    ],
    [
      "unknown error",
      { ...base, session: sessionFixture({ state: "ERROR", error: { code: "weird", message: "Disk on fire" } }) },
      "error",
    ],
    ["start rejected: worker", { ...base, startErrorCode: "worker_unavailable" }, "worker_offline"],
    ["start rejected: model", { ...base, startErrorCode: "model_unavailable" }, "model_unavailable"],
    ["start rejected: busy", { ...base, startErrorCode: "session_active" }, "error"],
  ])("%s", (_name, input, expected) => {
    expect(describeView(input).key).toBe(expected);
  });

  it("never claims processing before the backend says RUNNING", () => {
    for (const state of ["STARTING", "STOPPING"] as const) {
      expect(describeView({ ...base, session: sessionFixture({ state }) }).key).not.toBe("processing");
    }
  });

  it("gives every state actionable, non-empty copy and never mentions visitors", () => {
    const inputs = [
      { ...base, sources: undefined, sourcesFailed: true },
      { ...base, sources: sourcesFixture({ sources: [] }) },
      base,
      { ...base, session: sessionFixture({ state: "RUNNING" }) },
      {
        ...base,
        session: sessionFixture({
          state: "ERROR",
          error: { code: "x", message: "The worker reported an unexpected failure." },
        }),
      },
      { ...base, startErrorCode: "worker_unavailable" },
    ];
    for (const i of inputs) {
      const v = describeView(i);
      expect(v.title.length).toBeGreaterThan(5);
      expect(v.body.length).toBeGreaterThan(20);
      expect(`${v.title} ${v.body}`).not.toMatch(/visitor/i);
    }
  });
});

describe("everyNth (frame_skip wording)", () => {
  it.each([
    [1, "every frame"],
    [2, "every 2nd frame"],
    [3, "every 3rd frame"],
    [4, "every 4th frame"],
    [11, "every 11th frame"],
    [21, "every 21st frame"],
  ])("%i -> %s", (n, text) => {
    expect(everyNth(n)).toBe(text);
  });
});
