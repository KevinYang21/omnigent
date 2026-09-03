import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useFileDropTarget } from "./useFileDropTarget";

/** A dataTransfer for an OS file drag; `types` is the mid-drag file signal. */
function fileDrag(files: File[] = []) {
  return { types: ["Files"], files };
}

/**
 * A drop target ("the chat column") with a child inside it and a sibling
 * outside it — the shell around the chat, which must keep its own behavior.
 */
function Harness({ onFiles }: { onFiles: (files: File[]) => void }) {
  const [target, setTarget] = useState<HTMLElement | null>(null);
  const active = useFileDropTarget(target, onFiles);
  return (
    <div>
      <div ref={setTarget} data-testid="target">
        <div data-testid="inside">transcript</div>
        <div data-testid="state">{active ? "active" : "idle"}</div>
      </div>
      <div data-testid="outside">sidebar</div>
    </div>
  );
}

function state(): string {
  return screen.getByTestId("state").textContent ?? "";
}

afterEach(cleanup);

describe("useFileDropTarget", () => {
  it("attaches files dropped anywhere inside the target", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const file = new File(["x"], "shot.png", { type: "image/png" });

    fireEvent.drop(screen.getByTestId("inside"), { dataTransfer: fileDrag([file]) });

    expect(onFiles).toHaveBeenCalledWith([file]);
  });

  // The point of scoping: the shell around the chat (sidebar, workspace rail)
  // is not an attachment surface, so a drop there is left to whatever owns it.
  it("ignores a drop outside the target", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const file = new File(["x"], "shot.png", { type: "image/png" });

    fireEvent.dragEnter(screen.getByTestId("outside"), { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
    const dropped = fireEvent.drop(screen.getByTestId("outside"), {
      dataTransfer: fileDrag([file]),
    });

    // Not default-prevented either: nothing claimed the drop.
    expect(dropped).toBe(true);
    expect(onFiles).not.toHaveBeenCalled();
  });

  it("reports the drag while it is over the target and clears it on drop", () => {
    render(<Harness onFiles={vi.fn()} />);
    expect(state()).toBe("idle");
    fireEvent.dragEnter(screen.getByTestId("target"), { dataTransfer: fileDrag() });
    expect(state()).toBe("active");
    fireEvent.drop(screen.getByTestId("target"), {
      dataTransfer: fileDrag([new File(["x"], "a.txt")]),
    });
    expect(state()).toBe("idle");
  });

  // Enter/leave fire in pairs as the pointer crosses child elements. Without
  // the depth count the affordance would flicker off the moment the drag
  // moved from the column onto the transcript inside it.
  it("keeps the drag active while the pointer crosses child elements", () => {
    render(<Harness onFiles={vi.fn()} />);
    const target = screen.getByTestId("target");
    const inside = screen.getByTestId("inside");

    fireEvent.dragEnter(target, { dataTransfer: fileDrag() });
    fireEvent.dragEnter(inside, { dataTransfer: fileDrag() });
    fireEvent.dragLeave(target, { dataTransfer: fileDrag() });
    expect(state()).toBe("active");

    // Leaving the last entered element ends the drag.
    fireEvent.dragLeave(inside, { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
  });

  it("clears the drag when it is cancelled", () => {
    render(<Harness onFiles={vi.fn()} />);
    fireEvent.dragEnter(screen.getByTestId("target"), { dataTransfer: fileDrag() });
    fireEvent.dragEnd(screen.getByTestId("target"), { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
  });

  // A text or link drag keeps its native meaning — dragging selected text
  // into the composer textarea has to keep working.
  it("ignores a drag that carries no files", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const transfer = { types: ["text/plain"], files: [] };

    fireEvent.dragEnter(screen.getByTestId("inside"), { dataTransfer: transfer });
    expect(state()).toBe("idle");
    const dropped = fireEvent.drop(screen.getByTestId("inside"), { dataTransfer: transfer });
    // Not default-prevented: the browser still handles the text drop itself.
    expect(dropped).toBe(true);
    expect(onFiles).not.toHaveBeenCalled();
  });

  // Without preventDefault on dragover the drop event never fires, and the
  // browser navigates away from the app to render the dropped file.
  it("claims a file drag so the browser does not open the file", () => {
    render(<Harness onFiles={vi.fn()} />);
    const target = screen.getByTestId("target");
    expect(fireEvent.dragOver(target, { dataTransfer: fileDrag() })).toBe(false);
    expect(state()).toBe("active");
  });

  it("stops listening once unmounted", () => {
    const onFiles = vi.fn();
    const view = render(<Harness onFiles={onFiles} />);
    const target = screen.getByTestId("target");
    view.unmount();

    fireEvent.drop(target, { dataTransfer: fileDrag([new File(["x"], "a.txt")]) });

    expect(onFiles).not.toHaveBeenCalled();
  });
});
