import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useWindowFileDrop } from "./useWindowFileDrop";

/** A dataTransfer for an OS file drag; `types` is the mid-drag file signal. */
function fileDrag(files: File[] = []) {
  return { types: ["Files"], files };
}

function Harness({ onFiles }: { onFiles: (files: File[]) => void }) {
  const active = useWindowFileDrop(onFiles);
  return <div data-testid="state">{active ? "active" : "idle"}</div>;
}

function state(): string {
  return screen.getByTestId("state").textContent ?? "";
}

afterEach(cleanup);

describe("useWindowFileDrop", () => {
  it("attaches files dropped anywhere on the page", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const file = new File(["x"], "shot.png", { type: "image/png" });

    fireEvent.drop(document.body, { dataTransfer: fileDrag([file]) });

    expect(onFiles).toHaveBeenCalledWith([file]);
  });

  it("reports the drag while it is over the page and clears it on drop", () => {
    render(<Harness onFiles={vi.fn()} />);
    expect(state()).toBe("idle");
    fireEvent.dragEnter(document.body, { dataTransfer: fileDrag() });
    expect(state()).toBe("active");
    fireEvent.drop(document.body, { dataTransfer: fileDrag([new File(["x"], "a.txt")]) });
    expect(state()).toBe("idle");
  });

  // Enter/leave fire in pairs as the pointer crosses child elements. Without
  // the depth count the affordance would flicker off the moment the drag
  // moved from the page onto anything nested inside it.
  it("keeps the drag active while the pointer crosses child elements", () => {
    render(<Harness onFiles={vi.fn()} />);
    const child = screen.getByTestId("state");

    fireEvent.dragEnter(document.body, { dataTransfer: fileDrag() });
    fireEvent.dragEnter(child, { dataTransfer: fileDrag() });
    fireEvent.dragLeave(document.body, { dataTransfer: fileDrag() });
    expect(state()).toBe("active");

    // Leaving the last entered element ends the drag.
    fireEvent.dragLeave(child, { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
  });

  it("clears the drag when it is cancelled", () => {
    render(<Harness onFiles={vi.fn()} />);
    fireEvent.dragEnter(document.body, { dataTransfer: fileDrag() });
    fireEvent.dragEnd(document.body, { dataTransfer: fileDrag() });
    expect(state()).toBe("idle");
  });

  // A text or link drag keeps its native meaning — dragging selected text
  // into the composer textarea has to keep working.
  it("ignores a drag that carries no files", () => {
    const onFiles = vi.fn();
    render(<Harness onFiles={onFiles} />);
    const transfer = { types: ["text/plain"], files: [] };

    fireEvent.dragEnter(document.body, { dataTransfer: transfer });
    expect(state()).toBe("idle");
    const dropped = fireEvent.drop(document.body, { dataTransfer: transfer });
    // Not default-prevented: the browser still handles the text drop itself.
    expect(dropped).toBe(true);
    expect(onFiles).not.toHaveBeenCalled();
  });

  // Without preventDefault on dragover the drop event never fires, and the
  // browser navigates away from the app to render the dropped file.
  it("claims a file drag so the browser does not open the file", () => {
    render(<Harness onFiles={vi.fn()} />);
    expect(fireEvent.dragOver(document.body, { dataTransfer: fileDrag() })).toBe(false);
    expect(state()).toBe("active");
  });

  it("stops listening once unmounted", () => {
    const onFiles = vi.fn();
    const view = render(<Harness onFiles={onFiles} />);
    view.unmount();

    fireEvent.drop(document.body, { dataTransfer: fileDrag([new File(["x"], "a.txt")]) });

    expect(onFiles).not.toHaveBeenCalled();
  });
});
