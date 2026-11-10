import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { useUiStore } from "@/lib/ui-store";

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

/** jsdom has no canvas: record 2D calls so tests can assert what was drawn and where. */
export interface CanvasCall {
  op: string;
  args: unknown[];
}
export const canvasCalls: CanvasCall[] = [];
function recordingContext(): CanvasRenderingContext2D {
  const record = (op: string) => (...args: unknown[]) => {
    canvasCalls.push({ op, args });
  };
  return {
    clearRect: record("clearRect"),
    drawImage: record("drawImage"),
    strokeRect: record("strokeRect"),
    fillRect: record("fillRect"),
    fillText: record("fillText"),
    measureText: (text: string) => ({ width: text.length * 7 }),
    setLineDash: record("setLineDash"),
    set lineWidth(v: number) {
      canvasCalls.push({ op: "lineWidth", args: [v] });
    },
    set font(_v: string) {},
    set textBaseline(_v: string) {},
    set strokeStyle(v: string) {
      canvasCalls.push({ op: "strokeStyle", args: [v] });
    },
    set fillStyle(_v: string) {},
  } as unknown as CanvasRenderingContext2D;
}
HTMLCanvasElement.prototype.getContext = function getContext() {
  return recordingContext();
} as unknown as HTMLCanvasElement["getContext"];

if (!window.matchMedia) {
  window.matchMedia = (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList;
}
Element.prototype.scrollIntoView ??= () => {};
Element.prototype.hasPointerCapture ??= () => false;
Element.prototype.releasePointerCapture ??= () => {};

beforeEach(() => {
  canvasCalls.length = 0;
  localStorage.clear();
  useUiStore.getState().reset();
  document.documentElement.classList.add("dark");
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
});
