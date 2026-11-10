import { render } from "@testing-library/react";
import { StrictMode, createRef } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VideoCanvas, type VideoCanvasHandle } from "@/features/monitoring/video-canvas";
import { frameHeader } from "./monitoring-mocks";
import { canvasCalls } from "./setup";

const bitmap = () => ({ width: 640, height: 360, close: vi.fn() }) as unknown as ImageBitmap & { close: ReturnType<typeof vi.fn> };

let originalRaf: typeof requestAnimationFrame;
beforeEach(() => {
  // Container 800x450 CSS px; jsdom reports 0 for layout so give the elements a size.
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 800 });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 450 });
  originalRaf = window.requestAnimationFrame;
  window.requestAnimationFrame = (cb) => {
    cb(0);
    return 1;
  };
});
afterEach(() => {
  window.requestAnimationFrame = originalRaf;
  Reflect.deleteProperty(HTMLElement.prototype, "clientWidth");
  Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
});

function mount() {
  const ref = createRef<VideoCanvasHandle>();
  const utils = render(<VideoCanvas ref={ref} label="preview" />);
  canvasCalls.length = 0;
  return { ref, ...utils };
}

const calls = (op: string) => canvasCalls.filter((c) => c.op === op).map((c) => c.args as number[]);

describe("VideoCanvas", () => {
  it("draws the frame letterboxed and maps boxes into canvas space", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), frameHeader({ frame_width: 400, frame_height: 400, faces: [{ bbox: [100, 100, 200, 200], confidence: 0.93 }] }));
    // 400x400 frame in an 800x450 canvas -> 450x450 starting at x=175.
    const [, dx, dy, dw, dh] = calls("drawImage")[0]!;
    expect([dx, dy, dw, dh]).toEqual([175, 0, 450, 450]);
    expect(calls("strokeRect")[0]).toEqual([175 + 112.5, 112.5, 112.5, 112.5]);
  });

  it("labels each box with its confidence", () => {
    const { ref } = mount();
    ref.current!.draw(
      bitmap(),
      frameHeader({
        faces: [
          { bbox: [100, 50, 200, 150], confidence: 0.93 },
          { bbox: [300, 60, 380, 160], confidence: 0.71 },
        ],
      }),
    );
    const labels = canvasCalls.filter((c) => c.op === "fillText").map((c) => c.args[0]);
    expect(labels).toEqual(["93%", "71%"]);
    expect(calls("strokeRect")).toHaveLength(2);
  });

  it("draws just the image when no face was found", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), frameHeader({ faces: [] }));
    expect(calls("drawImage")).toHaveLength(1);
    expect(calls("strokeRect")).toHaveLength(0);
  });

  it("keeps labels inside the video area for boxes at the top edge", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), frameHeader({ faces: [{ bbox: [10, 0, 110, 100], confidence: 0.9 }] }));
    const [, ty] = calls("fillRect")[0]!;
    expect(ty).toBeGreaterThanOrEqual(0);
  });

  it("closes superseded bitmaps so memory does not grow", () => {
    const { ref } = mount();
    const first = bitmap();
    const second = bitmap();
    ref.current!.draw(first, frameHeader());
    ref.current!.draw(second, frameHeader());
    expect(first.close).toHaveBeenCalledTimes(1);
    expect(second.close).not.toHaveBeenCalled();
    ref.current!.clear();
    expect(second.close).toHaveBeenCalledTimes(1);
  });

  it("clear() erases the canvas and draws nothing", () => {
    const { ref } = mount();
    ref.current!.draw(bitmap(), frameHeader());
    canvasCalls.length = 0;
    ref.current!.clear();
    expect(calls("clearRect")).toHaveLength(1);
    expect(calls("drawImage")).toHaveLength(0);
  });

  it("exposes an accessible name", () => {
    const { getByRole } = mount();
    expect(getByRole("img", { name: "preview" })).toBeInTheDocument();
  });

  it("still paints under React StrictMode (effects mount, clean up and mount again)", () => {
    // Regression: cancelling the first scheduled frame during StrictMode's simulated unmount left
    // the coalescing flag set, so nothing was ever painted. Model a real browser: callbacks queue
    // up, can be cancelled, and run when the "frame" is flushed.
    const queue = new Map<number, FrameRequestCallback>();
    let nextId = 1;
    window.requestAnimationFrame = (cb) => {
      queue.set(nextId, cb);
      return nextId++;
    };
    window.cancelAnimationFrame = (id) => {
      queue.delete(id);
    };
    const flush = () => {
      const pending = [...queue.values()];
      queue.clear();
      pending.forEach((cb) => cb(0));
    };
    const ref = createRef<VideoCanvasHandle>();
    render(
      <StrictMode>
        <VideoCanvas ref={ref} label="preview" />
      </StrictMode>,
    );
    flush();
    canvasCalls.length = 0;
    ref.current!.draw(bitmap(), frameHeader());
    flush();
    expect(calls("drawImage")).toHaveLength(1);
    expect(calls("strokeRect")).toHaveLength(1);
  });
});
