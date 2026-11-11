import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EvidenceImage } from "@/features/events/evidence-image";
import { objectUrl } from "@/lib/object-url";

const create = vi.fn(() => "blob:one");
const revoke = vi.fn();

beforeEach(() => {
  create.mockClear();
  revoke.mockClear();
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
});

describe("object URLs for authenticated images", () => {
  it("creates one URL per blob and reuses it", () => {
    const blob = new Blob(["x"]);
    expect(objectUrl(blob)).toBe(objectUrl(blob));
    expect(create).toHaveBeenCalledTimes(1);
    create.mockReturnValueOnce("blob:two");
    expect(objectUrl(new Blob(["y"]))).toBe("blob:two");
  });

  it("keeps the image alive under StrictMode when the data is already cached", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["event-image", "e".repeat(32)], new Blob([new Uint8Array([0xff, 0xd8])], { type: "image/jpeg" }));
    render(
      <StrictMode>
        <QueryClientProvider client={client}>
          <EvidenceImage eventId={"e".repeat(32)} status="STORED" alt="cached evidence" />
        </QueryClientProvider>
      </StrictMode>,
    );
    const img = await screen.findByRole("img", { name: "cached evidence" });
    expect(img).toHaveAttribute("src", "blob:one");
    // the simulated unmount of StrictMode must NOT have revoked the URL that is still on screen
    expect(revoke).not.toHaveBeenCalled();
  });
});
