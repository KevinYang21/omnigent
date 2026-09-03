import { act, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { MemoryRouter, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { findWorkspaceLeafBySession, useWorkspaceLayoutStore } from "@/store/workspaceLayout";
import {
  SessionDragDropProvider,
  useSessionDragDrop,
  type SessionDragState,
} from "./SessionDragDropProvider";

let dndProps: Record<string, (event: unknown) => void> = {};

vi.mock("@dnd-kit/core", () => ({
  DndContext: ({ children, ...props }: { children: React.ReactNode }) => {
    dndProps = props as typeof dndProps;
    return children;
  },
  DragOverlay: ({ children }: { children: React.ReactNode }) => children,
  MouseSensor: function MouseSensor() {},
  pointerWithin: () => [],
  TouchSensor: function TouchSensor() {},
  useSensor: () => ({}),
  useSensors: () => [],
}));

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>;
}

function SidebarDropProbe({
  onDrop,
}: {
  onDrop: (drag: SessionDragState, target: unknown) => void;
}) {
  const { registerSidebarDropHandler } = useSessionDragDrop();
  useEffect(
    () => registerSidebarDropHandler((drag, target) => onDrop(drag, target)),
    [onDrop, registerSidebarDropHandler],
  );
  return null;
}

describe("SessionDragDropProvider", () => {
  beforeEach(() => {
    dndProps = {};
    localStorage.clear();
    useWorkspaceLayoutStore.getState().reset("session-a");
  });

  it("splits the target pane and navigates to the dragged session", () => {
    render(
      <MemoryRouter initialEntries={["/c/session-a"]}>
        <SessionDragDropProvider>
          <LocationProbe />
        </SessionDragDropProvider>
      </MemoryRouter>,
    );

    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    const active = {
      id: "session-b",
      data: { current: { type: "session", label: "Session B", project: null, isPinned: false } },
    };

    act(() => {
      dndProps.onDragStart?.({ active });
      dndProps.onDragEnd?.({
        active,
        over: {
          data: { current: { type: "workspace-pane", paneId: targetPaneId, edge: "right" } },
        },
      });
    });

    const state = useWorkspaceLayoutStore.getState();
    expect(findWorkspaceLeafBySession(state.root, "session-b")?.id).toBe(state.focusedPaneId);
    expect(screen.getByTestId("location")).toHaveTextContent("/c/session-b");
  });

  it("delegates existing sidebar drop targets unchanged", () => {
    const onDrop = vi.fn();
    render(
      <MemoryRouter>
        <SessionDragDropProvider>
          <SidebarDropProbe onDrop={onDrop} />
        </SessionDragDropProvider>
      </MemoryRouter>,
    );

    const active = {
      id: "session-a",
      data: { current: { type: "session", label: "Session A", project: null, isPinned: false } },
    };
    const target = { type: "project", name: "Project Alpha" };

    act(() => {
      dndProps.onDragStart?.({ active });
      dndProps.onDragEnd?.({ active, over: { data: { current: target } } });
    });

    expect(onDrop).toHaveBeenCalledWith(
      expect.objectContaining({ id: "session-a", label: "Session A" }),
      target,
    );
  });
});
