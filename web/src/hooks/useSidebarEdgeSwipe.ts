// Web left-edge swipe that drives the sidebar as an interactive drawer.
//
// The iOS native shell already streams a left-edge swipe over the native bridge
// (see `onNativeSidebarDrag`), but a plain mobile browser has no such gesture —
// there the only way to reveal the sidebar was the header button. This hook adds
// the same drawer feel with DOM touch events, emitting the SAME (progress →
// settle) contract AppShell already consumes, so both paths share one renderer.
//
// It is gated on the device being touch-primary (`pointer: coarse`), NOT on the
// viewport width: capability, not screen size, is what decides whether a swipe
// makes sense. Callers pass `enabled` (typically `useIsCoarsePointer()`).

import { useEffect } from "react";

// Left-edge band (CSS px) within which a swipe may BEGIN opening the drawer.
// Wide enough to hit reliably with a thumb, narrow enough not to swallow taps
// on left-aligned content.
const EDGE_ZONE_PX = 28;
// Movement (CSS px) before we commit to "this is a horizontal drawer drag"
// rather than a tap or a vertical scroll. Below this we stay undecided.
const DECIDE_SLOP_PX = 12;
// Flick speed (CSS px/ms) that settles the drawer in the swipe's direction
// regardless of how far it travelled — a fast short flick still opens/closes.
const FLICK_VELOCITY = 0.35;
// Strip of chat left visible to the right of the mobile drawer (Tailwind
// `right-14` = 3.5rem). The drawer's own width — the denominator that maps
// finger travel to a 0→1 open fraction — is the viewport minus this strip.
const PEEK_STRIP_PX = 56;

export interface SidebarEdgeSwipeOptions {
  /** Master switch — pass a touch-capability signal, e.g. `useIsCoarsePointer()`. */
  enabled: boolean;
  /** Whether the sidebar is currently open (decides swipe direction & baseline). */
  isOpen: boolean;
  /**
   * Live open fraction (0→1) during a drag, or `null` on release. Mirrors the
   * native bridge's begin/move frames; wire it to the same drag-progress state
   * the iOS path uses so the drawer tracks the finger 1:1.
   */
  onDragProgress: (progress: number | null) => void;
  /** Settle decision on release — the drawer animates to this resting state. */
  onSettle: (open: boolean) => void;
}

/**
 * Attach a left-edge swipe gesture (touch) that opens the sidebar by dragging
 * right and closes it by dragging left. No-op when `enabled` is false or off a
 * touch device. Emits `onDragProgress` while the finger moves and `onSettle` on
 * release; the caller owns the visual (see AppShell / Sidebar `dragProgress`).
 */
export function useSidebarEdgeSwipe({
  enabled,
  isOpen,
  onDragProgress,
  onSettle,
}: SidebarEdgeSwipeOptions): void {
  useEffect(() => {
    if (!enabled || typeof window === "undefined") return;

    let tracking = false; // a candidate gesture is being watched
    let dragging = false; // committed to a horizontal drawer drag
    let startX = 0;
    let startY = 0;
    let drawerWidth = 1; // px; finger-travel denominator, set on start
    let progress = isOpen ? 1 : 0;
    let lastX = 0;
    let lastT = 0;
    let velocity = 0; // px/ms, signed (rightward positive)

    const clamp01 = (n: number) => (n < 0 ? 0 : n > 1 ? 1 : n);

    function reset() {
      tracking = false;
      dragging = false;
      velocity = 0;
    }

    function onTouchStart(e: TouchEvent) {
      // Multi-touch (pinch/zoom) isn't a drawer swipe.
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      // A closed drawer only opens from the screen's left edge; an open drawer
      // can be swiped shut from anywhere over it.
      if (!isOpen && t.clientX > EDGE_ZONE_PX) return;
      tracking = true;
      dragging = false;
      startX = t.clientX;
      lastX = t.clientX;
      startY = t.clientY;
      lastT = e.timeStamp;
      velocity = 0;
      progress = isOpen ? 1 : 0;
      drawerWidth = Math.max(1, window.innerWidth - PEEK_STRIP_PX);
    }

    function onTouchMove(e: TouchEvent) {
      if (!tracking) return;
      const t = e.touches[0];
      const dx = t.clientX - startX;
      const dy = t.clientY - startY;

      if (!dragging) {
        // Wait for enough travel to tell a swipe from a tap.
        if (Math.abs(dx) < DECIDE_SLOP_PX && Math.abs(dy) < DECIDE_SLOP_PX) return;
        // Vertical intent (scrolling the session list) — hand it back.
        if (Math.abs(dy) >= Math.abs(dx)) {
          tracking = false;
          return;
        }
        // Direction must match the actionable move: open ⇒ drag right,
        // close ⇒ drag left. A wrong-way swipe isn't ours.
        if (!isOpen && dx <= 0) {
          tracking = false;
          return;
        }
        if (isOpen && dx >= 0) {
          tracking = false;
          return;
        }
        dragging = true;
      }

      const now = e.timeStamp;
      const dt = now - lastT;
      if (dt > 0) velocity = (t.clientX - lastX) / dt;
      lastX = t.clientX;
      lastT = now;

      progress = clamp01((isOpen ? 1 : 0) + dx / drawerWidth);
      onDragProgress(progress);
      // We own the gesture now — stop the page scrolling under the drawer.
      if (e.cancelable) e.preventDefault();
    }

    function onTouchEnd() {
      if (dragging) {
        // Settle on a flick (fast in either direction) or, absent a flick, on
        // whichever resting state the drawer is closer to.
        const open =
          velocity > FLICK_VELOCITY ? true : velocity < -FLICK_VELOCITY ? false : progress > 0.5;
        onDragProgress(null);
        onSettle(open);
      }
      reset();
    }

    // touchmove must be non-passive so preventDefault can suppress scroll once
    // we've committed to a horizontal drag.
    const moveOpts: AddEventListenerOptions = { passive: false };
    window.addEventListener("touchstart", onTouchStart, { passive: true });
    window.addEventListener("touchmove", onTouchMove, moveOpts);
    window.addEventListener("touchend", onTouchEnd, { passive: true });
    window.addEventListener("touchcancel", onTouchEnd, { passive: true });
    return () => {
      window.removeEventListener("touchstart", onTouchStart);
      window.removeEventListener("touchmove", onTouchMove, moveOpts);
      window.removeEventListener("touchend", onTouchEnd);
      window.removeEventListener("touchcancel", onTouchEnd);
    };
  }, [enabled, isOpen, onDragProgress, onSettle]);
}
