// Chat-scoped file drop: a file dragged in from the OS attaches to the composer
// wherever it lands inside the chat column — the transcript, the empty space
// beside the composer, the composer box itself — not just on the box. The
// surrounding shell (sidebar, workspace rail) is deliberately excluded: a file
// dropped there is not a chat attachment.
//
// Only drags that carry files are claimed — a text or link drag keeps its
// native behavior, so dragging selected text into the textarea still works.

import { useEffect, useRef, useState } from "react";

/** True when the drag carries OS files rather than text, a link, or HTML. */
function carriesFiles(transfer: DataTransfer | null | undefined): boolean {
  if (!transfer) return false;
  // `types` is a frozen string array in modern browsers, a DOMStringList in
  // older ones — Array.from covers both. Files are only readable on drop, so
  // this is the only signal available during the drag.
  return Array.from(transfer.types ?? []).includes("Files");
}

/**
 * Bind file drag-and-drop on ``target`` and report whether such a drag is in
 * flight over it (for a drop affordance). ``onFiles`` receives the dropped
 * files. A null target binds nothing.
 */
export function useFileDropTarget(
  target: HTMLElement | null,
  onFiles: (files: File[]) => void,
): boolean {
  const [isDragActive, setIsDragActive] = useState(false);
  // Keep the callback in a ref so re-renders don't re-bind the listeners
  // mid-drag (a rebind between dragenter and drop loses the depth count).
  const onFilesRef = useRef(onFiles);
  onFilesRef.current = onFiles;

  useEffect(() => {
    if (!target) return;
    // Enter/leave fire in pairs as the pointer crosses child elements; count
    // them so moving between children doesn't clear the affordance.
    let depth = 0;

    const enter = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      depth += 1;
      setIsDragActive(true);
    };

    const leave = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setIsDragActive(false);
    };

    const over = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      // Required for a drop event to fire at all, and it also stops the
      // browser from opening the file over the app.
      e.preventDefault();
      // Safety net: a drag entering from another window can reach us without
      // a matching dragenter.
      setIsDragActive(true);
    };

    const drop = (e: DragEvent): void => {
      if (!carriesFiles(e.dataTransfer)) return;
      e.preventDefault();
      depth = 0;
      setIsDragActive(false);
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length > 0) onFilesRef.current(files);
    };

    // A cancelled drag (Esc, or a drop outside the target) ends here.
    const end = (): void => {
      depth = 0;
      setIsDragActive(false);
    };

    target.addEventListener("dragenter", enter);
    target.addEventListener("dragleave", leave);
    target.addEventListener("dragover", over);
    target.addEventListener("drop", drop);
    target.addEventListener("dragend", end);
    return () => {
      target.removeEventListener("dragenter", enter);
      target.removeEventListener("dragleave", leave);
      target.removeEventListener("dragover", over);
      target.removeEventListener("drop", drop);
      target.removeEventListener("dragend", end);
      setIsDragActive(false);
    };
  }, [target]);

  return isDragActive;
}
