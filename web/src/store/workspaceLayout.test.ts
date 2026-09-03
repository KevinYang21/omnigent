import { beforeEach, describe, expect, it } from "vitest";
import {
  WORKSPACE_LAYOUT_STORAGE_KEY,
  closeWorkspacePane,
  createWorkspaceLayout,
  findWorkspaceLeaf,
  readWorkspaceLayout,
  resizeWorkspaceSplit,
  selectWorkspaceSession,
  splitWorkspacePane,
  writeWorkspaceLayout,
} from "./workspaceLayout";

describe("workspace layout transitions", () => {
  it("creates horizontal and vertical splits around the target pane", () => {
    const initial = createWorkspaceLayout("session-a");
    const targetId = initial.root.id;

    const right = splitWorkspacePane(initial, targetId, "session-b", "right");
    expect(right.root).toMatchObject({
      kind: "split",
      direction: "horizontal",
      sizes: [50, 50],
      children: [
        { kind: "leaf", sessionId: "session-a" },
        { kind: "leaf", sessionId: "session-b" },
      ],
    });
    expect(findWorkspaceLeaf(right.root, right.focusedPaneId)?.sessionId).toBe("session-b");

    const leftPaneId = right.root.kind === "split" ? right.root.children[0].id : "";
    const top = splitWorkspacePane(right, leftPaneId, "session-c", "top");
    expect(top.root).toMatchObject({
      kind: "split",
      direction: "horizontal",
      children: [
        {
          kind: "split",
          direction: "vertical",
          children: [
            { kind: "leaf", sessionId: "session-c" },
            { kind: "leaf", sessionId: "session-a" },
          ],
        },
        { kind: "leaf", sessionId: "session-b" },
      ],
    });
  });

  it("selects existing sessions, replaces the focused pane, and avoids duplicates", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");

    const selectedExisting = selectWorkspaceSession(split, "session-a");
    expect(
      findWorkspaceLeaf(selectedExisting.root, selectedExisting.focusedPaneId)?.sessionId,
    ).toBe("session-a");

    const replaced = selectWorkspaceSession(selectedExisting, "session-c");
    expect(findWorkspaceLeaf(replaced.root, replaced.focusedPaneId)?.sessionId).toBe("session-c");
    expect(JSON.stringify(replaced.root)).not.toContain("session-a");

    const duplicateDrop = splitWorkspacePane(
      replaced,
      replaced.focusedPaneId,
      "session-b",
      "bottom",
    );
    expect(duplicateDrop).toEqual(replaced);
  });

  it("collapses a split when a pane closes and keeps a valid focus", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");

    const closed = closeWorkspacePane(split, split.focusedPaneId);
    expect(closed.root).toMatchObject({ kind: "leaf", sessionId: "session-a" });
    expect(closed.focusedPaneId).toBe(closed.root.id);

    expect(closeWorkspacePane(closed, closed.root.id)).toEqual(closed);
  });

  it("clamps resized split ratios", () => {
    const initial = createWorkspaceLayout("session-a");
    const split = splitWorkspacePane(initial, initial.root.id, "session-b", "right");
    if (split.root.kind !== "split") throw new Error("expected split root");

    expect(resizeWorkspaceSplit(split, split.root.id, 10).root).toMatchObject({ sizes: [20, 80] });
    expect(resizeWorkspaceSplit(split, split.root.id, 72).root).toMatchObject({ sizes: [72, 28] });
    expect(resizeWorkspaceSplit(split, split.root.id, 95).root).toMatchObject({ sizes: [80, 20] });
  });
});

describe("workspace layout persistence", () => {
  beforeEach(() => localStorage.clear());

  it("round-trips a versioned layout and rejects malformed state", () => {
    const initial = createWorkspaceLayout("session-a");
    const layout = splitWorkspacePane(initial, initial.root.id, "session-b", "bottom");

    writeWorkspaceLayout(layout);
    expect(readWorkspaceLayout()).toEqual(layout);

    localStorage.setItem(WORKSPACE_LAYOUT_STORAGE_KEY, JSON.stringify({ version: 1, root: null }));
    expect(readWorkspaceLayout()).toBeNull();
  });
});
