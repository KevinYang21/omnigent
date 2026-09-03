// Full-page affordance for a file drag: the whole viewport is the drop target
// (see useWindowFileDrop), so the cue has to be page-wide rather than a badge
// on the composer box.
//
// Portalled to <body> so a transformed ancestor can't re-anchor `fixed`, and
// pointer-events-none so it never sits between the drag and the page.

import { createPortal } from "react-dom";

export function FileDropOverlay({ label = "Drop files here" }: { label?: string }) {
  return createPortal(
    <div
      className="pointer-events-none fixed inset-0 z-[70] flex items-center justify-center bg-background/60 backdrop-blur-[2px]"
      data-testid="file-drop-overlay"
    >
      <div className="absolute inset-3 rounded-2xl border-2 border-dashed border-ring" />
      <span className="rounded-full border border-border bg-card px-4 py-2 text-ui font-medium text-ring shadow-composer">
        {label}
      </span>
    </div>,
    document.body,
  );
}
