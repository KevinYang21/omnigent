// Warms the `["session", id]` snapshot cache while the user skims the sidebar,
// so consumers that read it with `staleTime: Infinity` — `useSession`
// (permission level), the Agents-rail root walk, the chat header/pickers —
// hit a warm cache the moment the row is opened instead of fetching then.
//
// NOTE: this does NOT short-circuit `bindStream`'s own load. Bind refetches the
// snapshot with `staleTime: 0` (and `refresh_state=true`) on purpose — a cached
// snapshot can miss items committed while another conversation was open — so it
// always re-reads regardless of what's warmed. The prefetch therefore uses the
// LIGHT read (no `refresh_state`): warming the cheap snapshot for the
// Infinity-staleTime consumers, without paying for the heavy runner-state
// refresh that bind will redo anyway.
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
  private readonly queryClient: QueryClient;

  constructor(queryClient: QueryClient) {
    this.queryClient = queryClient;
  }

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
        queryFn: () => getSessionSlim(conversationId),
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
