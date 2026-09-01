import { render } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSidebarEdgeSwipe } from "./useSidebarEdgeSwipe";

// jsdom has no TouchEvent constructor and its Event has no `timeStamp` we can
// set, so fabricate a minimal touch event the hook reads: `touches`,
// `clientX/Y`, `timeStamp`, `cancelable`, and a spyable `preventDefault`.
function touchEvent(
  type: "touchstart" | "touchmove" | "touchend" | "touchcancel",
  { x = 0, y = 0, t = 0 }: { x?: number; y?: number; t?: number } = {},
): Event {
  const ev = new Event(type, { bubbles: true, cancelable: true });
  const touch = { clientX: x, clientY: y };
  Object.defineProperties(ev, {
    touches: { value: type === "touchend" || type === "touchcancel" ? [] : [touch] },
    timeStamp: { value: t },
  });
  return ev;
}

function fire(ev: Event) {
  act(() => {
    window.dispatchEvent(ev);
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
});
