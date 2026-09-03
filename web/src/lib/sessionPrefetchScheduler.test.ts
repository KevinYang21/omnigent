// Guards the scheduler's concurrency contract: at most one in-flight prefetch,
// the latest-queued id wins, and an already-cached id is skipped.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Session } from "@/lib/types";
import { getSessionSlim } from "@/lib/sessionsApi";
import { SessionPrefetchScheduler } from "./sessionPrefetchScheduler";

vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: vi.fn() }));
const mockGet = vi.mocked(getSessionSlim);

afterEach(() => vi.clearAllMocks());

function deferred() {
  let resolve!: (s: Session) => void;
  const promise = new Promise<Session>((r) => {
    resolve = r;
  });
  return { promise, resolve: () => resolve({ id: "x" } as Session) };
}

describe("SessionPrefetchScheduler", () => {
  it("keeps at most one prefetch in flight and runs only the latest queued", async () => {
    const a = deferred();
    mockGet.mockReturnValueOnce(a.promise).mockResolvedValue({ id: "y" } as Session);
    const s = new SessionPrefetchScheduler(new QueryClient());

    s.prefetch("conv_a"); // starts
    s.prefetch("conv_b"); // queued
    s.prefetch("conv_c"); // replaces conv_b as the queued one
    expect(mockGet).toHaveBeenCalledTimes(1);

    a.resolve();
    await vi.waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    // Second call is the latest-queued id, not the dropped conv_b.
    expect(mockGet.mock.calls[1]?.[0]).toBe("conv_c");
  });

  it("skips an id already cached fresh", () => {
    const qc = new QueryClient();
    qc.setQueryData(["session", "conv_a"], { id: "conv_a" });
    new SessionPrefetchScheduler(qc).prefetch("conv_a");
    expect(mockGet).not.toHaveBeenCalled();
  });
});
