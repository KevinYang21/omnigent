// One `SessionPrefetchScheduler` shared across all sidebar rows, so the
// "at most one in-flight prefetch while skimming" bound holds across rows
// (a per-row scheduler would let each row fetch independently).
//
// `useSessionRowPrefetch` returns handlers for a row's <Link>: hover is
// 100ms-debounced (skip rows the pointer just grazes), focus is immediate
// (keyboard skimming lands on the row deliberately).

import { type ReactNode, createContext, useContext, useEffect, useMemo, useRef } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { SessionPrefetchScheduler } from "@/lib/sessionPrefetchScheduler";

const HOVER_PREFETCH_DEBOUNCE_MS = 100;

const SessionPrefetchSchedulerContext = createContext<SessionPrefetchScheduler | null>(null);

export function SessionPrefetchSchedulerProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const scheduler = useMemo(() => new SessionPrefetchScheduler(queryClient), [queryClient]);
  return (
    <SessionPrefetchSchedulerContext.Provider value={scheduler}>
      {children}
    </SessionPrefetchSchedulerContext.Provider>
  );
}

/**
 * Handlers to warm a conversation row on hover/focus. Returns no-ops outside a
 * provider (the sidebar always wraps rows, but tests may render a bare row).
 */
export function useSessionRowPrefetch(conversationId: string): {
  onPointerEnter: () => void;
  onPointerLeave: () => void;
  onFocus: () => void;
} {
  const scheduler = useContext(SessionPrefetchSchedulerContext);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const clear = () => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  };
  useEffect(() => clear, []);
  return useMemo(
    () => ({
      onPointerEnter: () => {
        if (!scheduler) return;
        clear();
        timerRef.current = setTimeout(() => {
          scheduler.prefetch(conversationId);
        }, HOVER_PREFETCH_DEBOUNCE_MS);
      },
      // Pointer left before the debounce fired — don't warm a grazed row.
      onPointerLeave: clear,
      onFocus: () => scheduler?.prefetch(conversationId),
    }),
    [scheduler, conversationId],
  );
}
