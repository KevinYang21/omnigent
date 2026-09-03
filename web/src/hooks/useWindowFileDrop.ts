// Page-wide file drop: a file dragged in from the OS attaches to the composer
// wherever it lands, not only on the composer box itself. Dropping an image on
// the transcript (or anywhere else on the page) has no other meaning, and left
// unhandled the browser navigates away from the session to render the file.
//
// Only drags that carry files are claimed — a text or link drag keeps its
// native behavior, so dragging selected text into the textarea still works.
// A drop zone that wants a file for itself can stopPropagation before the
// event reaches window.

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
 * Bind window-level file drag-and-drop and report whether such a drag is in
 * flight (for a drop affordance). ``onFiles`` receives the dropped files.
 *
 * Bind once per page: two live instances would both attach the same drop.
 */
export function useWindowFileDrop(onFiles: (files: File[]) => void): boolean {
  const [isDragActive, setIsDragActive] = useState(false);
  // Keep the callback in a ref so re-renders don't re-bind the listeners
  // mid-drag (a rebind between dragenter and drop loses the depth count).
  const onFilesRef = useRef(onFiles);
  onFilesRef.current = onFiles;

  useEffect(() => {
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
      // Safety net: some drags (a drop from another window, or an enter
      // swallowed by an iframe) reach us without a matching dragenter.
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

    // A cancelled drag (Esc, or a drop outside the window) ends here.
    const end = (): void => {
      depth = 0;
      setIsDragActive(false);
    };

    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    window.addEventListener("dragend", end);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
      window.removeEventListener("dragend", end);
    };
  }, []);

  return isDragActive;
}
