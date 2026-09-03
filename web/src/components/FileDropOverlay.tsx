// Drop affordance for a file drag, covering the chat column it is portalled
// into (see useFileDropTarget) rather than the whole viewport — the shell
// around the chat is not a drop target.
//
// Portalled into the drop target itself so it spans the transcript and the
// composer together, and pointer-events-none so it never sits between the drag
// and the page.

import { createPortal } from "react-dom";

export function FileDropOverlay({
  container,
  label = "Drop files here",
}: {
  container: HTMLElement;
  label?: string;
}) {
  return createPortal(
    <div
      className="pointer-events-none absolute inset-0 z-40 flex items-center justify-center bg-background/60 backdrop-blur-[2px]"
      data-testid="file-drop-overlay"
    >
      <div className="absolute inset-3 rounded-2xl border-2 border-dashed border-ring" />
      <span className="rounded-full border border-border bg-card px-4 py-2 text-ui font-medium text-ring shadow-composer">
        {label}
      </span>
    </div>,
    container,
  );
}
