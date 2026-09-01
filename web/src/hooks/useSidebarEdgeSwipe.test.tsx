import { render } from "@testing-library/react";
import { act, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSidebarEdgeSwipe } from "./useSidebarEdgeSwipe";

// jsdom has no TouchEvent constructor and its Event has no `timeStamp` we can
// set, so fabricate a minimal touch event the hook reads: `touches`,
// `clientX/Y`, `timeStamp`, `cancelable`, and a spyable `preventDefault`.
interface TouchOpts {
  x?: number;
  y?: number;
  t?: number;
  /** Number of active touch points — >1 simulates a second finger (pinch). */
  count?: number;
}

function touchEvent(
  type: "touchstart" | "touchmove" | "touchend" | "touchcancel",
  { x = 0, y = 0, t = 0, count = 1 }: TouchOpts = {},
): Event {
  // Bubbles so a dispatch on a child element reaches the window listeners and
  // carries that element as `e.target` (used by the horizontal-scroller check).
  const ev = new Event(type, { bubbles: true, cancelable: true });
  const primary = { clientX: x, clientY: y };
  // A second point just needs to exist for length checks; its coords are unused.
  const touches = type === "touchend" || type === "touchcancel" ? [] : Array(count).fill(primary);
  Object.defineProperties(ev, {
    touches: { value: touches },
    timeStamp: { value: t },
  });
  return ev;
}

function fire(ev: Event, on: EventTarget = window) {
  act(() => {
    on.dispatchEvent(ev);
  });
}

interface HarnessProps {
  enabled?: boolean;
  isOpen?: boolean;
  onDragProgress: (p: number | null) => void;
  onSettle: (open: boolean) => void;
}

function Harness({ enabled = true, isOpen = false, onDragProgress, onSettle }: HarnessProps) {
  useSidebarEdgeSwipe({ enabled, isOpen, onDragProgress, onSettle });
  return null;
}

describe("useSidebarEdgeSwipe", () => {
  beforeEach(() => {
    // Wide viewport so drawerWidth (innerWidth - peek strip) is a stable 744px.
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("opens on a rightward edge swipe past the halfway point", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness onDragProgress={onDragProgress} onSettle={onSettle} />);

    fire(touchEvent("touchstart", { x: 5, y: 100, t: 0 }));
    // Slow drag well past half the 744px drawer, so velocity stays sub-flick
    // and the settle is decided by position (> 0.5), not by a flick.
    fire(touchEvent("touchmove", { x: 500, y: 100, t: 500 }));
    fire(touchEvent("touchend", { x: 500, y: 100, t: 500 }));

    expect(onDragProgress).toHaveBeenCalled();
    expect(onSettle).toHaveBeenCalledWith(true);
    // Progress reset to null on release.
    expect(onDragProgress).toHaveBeenLastCalledWith(null);
  });

  it("ignores a swipe that does not start at the left edge", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness onDragProgress={onDragProgress} onSettle={onSettle} />);

    fire(touchEvent("touchstart", { x: 200, y: 100, t: 0 }));
    fire(touchEvent("touchmove", { x: 600, y: 100, t: 100 }));
    fire(touchEvent("touchend", { x: 600, y: 100, t: 100 }));

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
  });

  it("hands back a vertical scroll gesture", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness onDragProgress={onDragProgress} onSettle={onSettle} />);

    fire(touchEvent("touchstart", { x: 5, y: 100, t: 0 }));
    // Mostly vertical — |dy| >= |dx|, so the drag is disowned.
    fire(touchEvent("touchmove", { x: 20, y: 300, t: 100 }));
    fire(touchEvent("touchend", { x: 20, y: 300, t: 100 }));

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
  });

  it("closes an open drawer on a fast leftward flick", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness isOpen onDragProgress={onDragProgress} onSettle={onSettle} />);

    fire(touchEvent("touchstart", { x: 400, y: 100, t: 0 }));
    // Short but fast leftward move: only 40px (< half) yet 40px/20ms = 2 px/ms,
    // well over the flick threshold, so it settles closed on velocity alone.
    fire(touchEvent("touchmove", { x: 360, y: 100, t: 20 }));
    fire(touchEvent("touchend", { x: 360, y: 100, t: 20 }));

    expect(onSettle).toHaveBeenCalledWith(false);
  });

  it("does nothing when disabled", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness enabled={false} onDragProgress={onDragProgress} onSettle={onSettle} />);

    fire(touchEvent("touchstart", { x: 5, y: 100, t: 0 }));
    fire(touchEvent("touchmove", { x: 500, y: 100, t: 500 }));
    fire(touchEvent("touchend", { x: 500, y: 100, t: 500 }));

    expect(onDragProgress).not.toHaveBeenCalled();
    expect(onSettle).not.toHaveBeenCalled();
  });

  it("does not claim a close-swipe that starts in a horizontal scroller", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness isOpen onDragProgress={onDragProgress} onSettle={onSettle} />);

    // A horizontally-scrollable element (e.g. a code block inside the drawer).
    const scroller = document.createElement("div");
    Object.defineProperties(scroller, {
      scrollWidth: { value: 1000, configurable: true },
      clientWidth: { value: 200, configurable: true },
    });
    vi.spyOn(window, "getComputedStyle").mockReturnValue({
      overflowX: "auto",
    } as CSSStyleDeclaration);
    document.body.appendChild(scroller);

    // Leftward drag starting on that element — the user scrolling the content.
    fire(touchEvent("touchstart", { x: 400, y: 100, t: 0 }), scroller);
    fire(touchEvent("touchmove", { x: 360, y: 100, t: 20 }), scroller);
    fire(touchEvent("touchend", { x: 360, y: 100, t: 20 }), scroller);

    expect(onSettle).not.toHaveBeenCalled();
    expect(onDragProgress).not.toHaveBeenCalled();
    document.body.removeChild(scroller);
  });

  it("ends the gesture when a second finger lands mid-drag", () => {
    const onDragProgress = vi.fn();
    const onSettle = vi.fn();
    render(<Harness onDragProgress={onDragProgress} onSettle={onSettle} />);

    fire(touchEvent("touchstart", { x: 5, y: 100, t: 0 }));
    // Commit the drag past halfway, then a second finger appears (pinch): the
    // gesture settles on what it had rather than tracking an arbitrary point.
    fire(touchEvent("touchmove", { x: 500, y: 100, t: 200 }));
    fire(touchEvent("touchmove", { x: 550, y: 100, t: 220, count: 2 }));

    expect(onSettle).toHaveBeenCalledWith(true);
    expect(onDragProgress).toHaveBeenLastCalledWith(null);
  });

  // Regression: AppShell passes an INLINE onSettle (fresh identity each render)
  // and re-renders on every drag frame because onDragProgress drives its state.
  // If the effect depended on the callbacks, that re-render would re-subscribe
  // the listeners and reset the in-flight gesture, freezing the drag after one
  // frame so touchend never settles. This harness reproduces both conditions:
  // a new onSettle closure each render, and a state bump on every progress tick.
  it("keeps tracking across re-renders driven by an inline callback (churn)", () => {
    const settle = vi.fn();
    function ChurnHarness() {
      // Bumped on every drag frame, mimicking AppShell's setSidebarDragProgress.
      const [, setProgress] = useState<number | null>(null);
      useSidebarEdgeSwipe({
        enabled: true,
        isOpen: false,
        onDragProgress: setProgress,
        // New identity every render — the exact pattern Polly flagged.
        onSettle: (open) => settle(open),
      });
      return null;
    }
    render(<ChurnHarness />);

    fire(touchEvent("touchstart", { x: 5, y: 100, t: 0 }));
    // Two separate move frames: the first re-renders the component (new onSettle
    // identity). If the gesture reset on that re-render, the second move would
    // see tracking===false and touchend would never settle.
    fire(touchEvent("touchmove", { x: 300, y: 100, t: 200 }));
    fire(touchEvent("touchmove", { x: 500, y: 100, t: 400 }));
    fire(touchEvent("touchend", { x: 500, y: 100, t: 400 }));

    expect(settle).toHaveBeenCalledWith(true);
  });
});
