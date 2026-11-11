import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "@/components/ui/toaster";
import { validateVideoFile } from "@/features/monitoring/upload-video";
import type { SourceOut } from "@/lib/api-types";
import { createController, installFakeStreaming, processingFetch, sourcesFixture } from "./monitoring-mocks";
import { renderApp } from "./render";

/** A controllable XMLHttpRequest: the test decides when progress arrives and how the server answers. */
class FakeXhr {
  static last: FakeXhr;
  static all: FakeXhr[] = [];
  method = "";
  url = "";
  headers: Record<string, string> = {};
  body: unknown = null;
  status = 0;
  responseText = "";
  aborted = false;
  upload: { onprogress: ((e: { lengthComputable: boolean; loaded: number; total: number }) => void) | null } = { onprogress: null };
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onabort: (() => void) | null = null;
  ontimeout: (() => void) | null = null;
  constructor() {
    FakeXhr.last = this;
    FakeXhr.all.push(this);
  }
  open(method: string, url: string) {
    this.method = method;
    this.url = url;
  }
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v;
  }
  send(body: unknown) {
    this.body = body;
  }
  abort() {
    this.aborted = true;
    this.onabort?.();
  }
  progress(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total });
  }
  respond(status: number, body: unknown) {
    this.status = status;
    this.responseText = JSON.stringify(body);
    this.onload?.();
  }
}

const uploaded: SourceOut = {
  id: `upload-${"a".repeat(32)}`,
  label: "Upload: lobby.mp4",
  type: "file",
  availability: "available",
  detail: null,
  origin: "upload",
};
const file = (name = "lobby.mp4", size = 2048) => new File([new Uint8Array(size)], name, { type: "video/mp4" });

function withSource(c: ReturnType<typeof createController>, add: boolean) {
  const list = c.sources;
  if (list === "error") throw new Error("sources are in error mode");
  c.sources = { ...list, sources: add ? [...list.sources, uploaded] : list.sources.filter((s) => s.id !== uploaded.id) };
}

function setup(applyAccept = true) {
  installFakeStreaming();
  const c = createController({ sources: sourcesFixture() });
  renderApp("/live", {}, processingFetch(c));
  return { c, user: userEvent.setup({ applyAccept }) };
}

describe("validateVideoFile", () => {
  it("accepts video types and refuses everything else, empty and absurdly large files", () => {
    expect(validateVideoFile({ name: "a.MP4", size: 10 })).toBeNull();
    expect(validateVideoFile({ name: "a.mkv", size: 10 })).toBeNull();
    expect(validateVideoFile({ name: "a.exe", size: 10 })).toMatch(/Unsupported/);
    expect(validateVideoFile({ name: "noextension", size: 10 })).toMatch(/Unsupported/);
    expect(validateVideoFile({ name: "a.mp4", size: 0 })).toMatch(/empty/);
    expect(validateVideoFile({ name: "a.mp4", size: 3 * 1024 ** 3 })).toMatch(/larger/);
  });
});

describe("uploading a video from the Live Monitoring page", () => {
  beforeEach(() => {
    FakeXhr.all = [];
    vi.stubGlobal("XMLHttpRequest", FakeXhr);
  });
  afterEach(() => vi.restoreAllMocks());

  it("offers the upload next to the sources, with a consent note", async () => {
    setup();
    const panel = await screen.findByTestId("upload-panel");
    expect(within(panel).getByTestId("upload-button")).toHaveTextContent("Choose file");
    expect(panel).toHaveTextContent(/Only upload videos you are allowed to process/);
    expect(panel).toHaveTextContent(/visitors are registered/);
    expect(screen.getByTestId("upload-input")).toHaveAttribute("accept", expect.stringContaining(".mp4"));
  });

  it("sends the file as the raw body with its name, shows progress, then selects the new source", async () => {
    const success = vi.spyOn(toast, "success").mockImplementation(() => "id");
    const { c, user } = setup();
    await screen.findByTestId("upload-panel");
    await user.upload(screen.getByTestId("upload-input"), file());
    const xhr = FakeXhr.last;
    expect(xhr.method).toBe("PUT");
    expect(xhr.url).toContain("/uploads?filename=lobby.mp4");
    expect(xhr.body).toBeInstanceOf(File);
    expect(xhr.headers["Content-Type"]).toBe("application/octet-stream");
    act(() => xhr.progress(512, 2048));
    expect(await screen.findByTestId("upload-progress")).toHaveTextContent("25%");
    expect(screen.getByLabelText("Upload progress")).toHaveAttribute("value", "0.25");
    withSource(c, true); // the server now lists it
    await act(async () => xhr.respond(201, uploaded));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Video uploaded", expect.objectContaining({ description: expect.stringContaining("lobby.mp4") })));
    expect(await screen.findByRole("radio", { name: /Upload: lobby\.mp4/ })).toBeChecked();
    expect(screen.queryByTestId("upload-progress")).not.toBeInTheDocument();
  });

  it("refuses an unsupported file before sending anything", async () => {
    const { user } = setup(false);
    await screen.findByTestId("upload-panel");
    await user.upload(screen.getByTestId("upload-input"), file("virus.exe"));
    expect(await screen.findByTestId("upload-error")).toHaveTextContent("Unsupported file type");
    expect(FakeXhr.all).toHaveLength(0);
  });

  it("shows the server's reason when the upload is rejected, and allows another try", async () => {
    const { user } = setup();
    await screen.findByTestId("upload-panel");
    await user.upload(screen.getByTestId("upload-input"), file("fake.mp4"));
    await act(async () => FakeXhr.last.respond(422, { error: { code: "not_a_video", message: "The file could not be opened as a video.", request_id: "r", details: null } }));
    expect(await screen.findByTestId("upload-error")).toHaveTextContent("could not be opened as a video");
    expect(screen.getByTestId("upload-button")).toBeEnabled();
    await user.upload(screen.getByTestId("upload-input"), file("second.mp4"));
    expect(FakeXhr.all).toHaveLength(2);
    expect(screen.queryByTestId("upload-error")).not.toBeInTheDocument(); // the old message is cleared
  });

  it("reports the size limit and a network failure", async () => {
    const { user } = setup();
    await screen.findByTestId("upload-panel");
    await user.upload(screen.getByTestId("upload-input"), file());
    await act(async () => FakeXhr.last.respond(413, { error: { code: "too_large", message: "The video exceeds the 500 MB limit.", request_id: "r", details: null } }));
    expect(await screen.findByTestId("upload-error")).toHaveTextContent("500 MB limit");
    await user.upload(screen.getByTestId("upload-input"), file());
    await act(async () => FakeXhr.last.onerror?.());
    expect(await screen.findByTestId("upload-error")).toHaveTextContent("Unable to reach the API");
  });

  it("can be cancelled without an error message", async () => {
    const { user } = setup();
    await screen.findByTestId("upload-panel");
    await user.upload(screen.getByTestId("upload-input"), file());
    await user.click(await screen.findByTestId("upload-cancel"));
    expect(FakeXhr.last.aborted).toBe(true);
    await waitFor(() => expect(screen.queryByTestId("upload-progress")).not.toBeInTheDocument());
    expect(screen.queryByTestId("upload-error")).not.toBeInTheDocument();
    expect(screen.getByTestId("upload-button")).toBeEnabled();
  });

  it("is disabled while a session is running (the source cannot change)", async () => {
    installFakeStreaming();
    const c = createController({ sources: sourcesFixture() });
    renderApp("/live", {}, processingFetch(c));
    const user = userEvent.setup();
    await screen.findByText("Ready to start");
    await user.click(screen.getByTestId("start-button"));
    await waitFor(() => expect(screen.getByTestId("upload-button")).toBeDisabled());
  });
});

describe("deleting an uploaded video", () => {
  beforeEach(() => vi.stubGlobal("XMLHttpRequest", FakeXhr));
  afterEach(() => vi.restoreAllMocks());

  it("asks for confirmation, calls the API and removes the source", async () => {
    installFakeStreaming();
    const c = createController({ sources: sourcesFixture() });
    withSource(c, true);
    const base = processingFetch(c);
    const calls: string[] = [];
    renderApp("/live", {}, (b) =>
      (async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), "http://localhost");
        if (url.pathname.includes("/uploads/")) {
          calls.push(`${init?.method} ${url.pathname.replace(/^.*\/v1/, "")}`);
          withSource(c, false);
          return new Response(null, { status: 204 });
        }
        return base(b)(input, init);
      }) as typeof fetch,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Delete uploaded video lobby.mp4" }));
    expect(await screen.findByText("Delete this uploaded video?")).toBeInTheDocument();
    expect(calls).toHaveLength(0); // nothing is deleted before confirming
    await user.click(screen.getByRole("button", { name: "Delete video" }));
    await waitFor(() => expect(calls).toEqual([`DELETE /uploads/${"a".repeat(32)}`]));
    await waitFor(() => expect(screen.queryByRole("radio", { name: /Upload: lobby/ })).not.toBeInTheDocument());
  });

  it("only uploaded videos get a delete button", async () => {
    installFakeStreaming();
    const c = createController({ sources: sourcesFixture() });
    withSource(c, true);
    renderApp("/live", {}, processingFetch(c));
    await screen.findByRole("radio", { name: /Upload: lobby/ });
    expect(screen.getAllByRole("button", { name: /^Delete uploaded video/ })).toHaveLength(1);
  });
});
