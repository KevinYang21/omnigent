// Warms the `["session", id]` snapshot cache while the user skims the sidebar,
// so clicking a row opens an already-fetched conversation. The open path
// (chatStore.bindStream) fetches the SAME query key, so a warmed entry is a
// direct cache hit — no duplicate request.
//
// "At most one in-flight prefetch while skimming": a hover/focus over one row
// after another shouldn't fire a burst of concurrent fetches. The scheduler
// serializes them — a new request while one is in flight is queued as the
// single "next", replacing any earlier queued one (you only care about the row
// you're on now) — and skips ids already cached fresh.

import type { QueryClient } from "@tanstack/react-query";
import { getSessionSlim } from "@/lib/sessionsApi";

export class SessionPrefetchScheduler {
  private inFlight = false;
  private next: string | null = null;

  constructor(private readonly queryClient: QueryClient) {}

  /** Warm `["session", id]`. No-op if already fresh; queued if one is running. */
  prefetch(conversationId: string): void {
    // Already cached fresh — the open path will hit it, nothing to warm. Uses
    // the same staleTime: Infinity the query declares, so a warmed entry counts.
    if (this.queryClient.getQueryData(["session", conversationId]) != null) return;
    if (this.inFlight) {
      this.next = conversationId;
      return;
    }
    this.run(conversationId);
  }

  private run(conversationId: string): void {
    this.inFlight = true;
    void this.queryClient
      .prefetchQuery({
        queryKey: ["session", conversationId],
        queryFn: () => getSessionSlim(conversationId, { refreshState: true }),
        staleTime: Infinity,
      })
      .finally(() => {
        this.inFlight = false;
        const queued = this.next;
        this.next = null;
        if (queued != null) this.prefetch(queued);
      });
  }
}
