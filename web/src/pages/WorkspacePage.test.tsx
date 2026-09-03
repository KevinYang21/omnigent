import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { conversationRegistry } from "@/store/conversationRegistry";
import { useWorkspaceLayoutStore } from "@/store/workspaceLayout";
import { WorkspacePage } from "./WorkspacePage";

const chatStoreMocks = vi.hoisted(() => ({ switchTo: vi.fn(), loadInBackground: vi.fn() }));

vi.mock("@/store/chatStore", () => ({
  ChatStoreScopeProvider: ({ children }: { children: ReactNode }) => children,
  useChatStore: {
    getState: () => ({
      switchTo: chatStoreMocks.switchTo,
      loadInBackground: chatStoreMocks.loadInBackground,
    }),
  },
}));

vi.mock("@dnd-kit/core", () => ({
  useDroppable: () => ({ setNodeRef: vi.fn(), isOver: false }),
}));

vi.mock("./ChatPage", () => ({
  ChatPage: ({ conversationId, active = true }: { conversationId?: string; active?: boolean }) => (
    <div data-testid={`chat-${conversationId ?? "landing"}`} data-active={String(active)}>
      {conversationId ?? "landing"}
    </div>
  ),
}));

function LocationProbe() {
  return <div data-testid="location">{useLocation().pathname}</div>;
}

function renderWorkspace(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <LocationProbe />
      <Routes>
        <Route path="/" element={<WorkspacePage />} />
        <Route path="/c/:conversationId" element={<WorkspacePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("WorkspacePage", () => {
  beforeEach(() => {
    localStorage.clear();
    useWorkspaceLayoutStore.getState().reset("session-a");
  });

  it("renders independent panes, focuses by pointer, and collapses on close", async () => {
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));
    const split = useWorkspaceLayoutStore.getState();
    if (split.root.kind !== "split") throw new Error("expected split root");
    const firstPaneId = split.root.children[0].id;
    const secondPaneId = split.root.children[1].id;

    renderWorkspace("/c/session-b");

    expect(screen.getByTestId("chat-session-a")).toHaveAttribute("data-active", "false");
    expect(screen.getByTestId("chat-session-b")).toHaveAttribute("data-active", "true");

    fireEvent.pointerDown(screen.getByTestId(`workspace-pane-${firstPaneId}`));
    await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/c/session-a"));
    expect(screen.getByTestId("chat-session-a")).toHaveAttribute("data-active", "true");

    fireEvent.click(screen.getByLabelText(`Close pane session-b`));
    await waitFor(() => expect(screen.queryByTestId(`workspace-pane-${secondPaneId}`)).toBeNull());
    expect(screen.getByTestId("chat-session-a")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Close pane/ })).toBeNull();
  });

  it("binds a background stream for panes that are not focused", async () => {
    // After a reload only the route conversation is live; the other pane must
    // hydrate in the background instead of painting the focused transcript.
    chatStoreMocks.loadInBackground.mockClear();
    const targetPaneId = useWorkspaceLayoutStore.getState().root.id;
    act(() => useWorkspaceLayoutStore.getState().splitPane(targetPaneId, "session-b", "right"));

    const pinSpy = vi.spyOn(conversationRegistry, "pin");
    const unpinSpy = vi.spyOn(conversationRegistry, "unpin");
    const { unmount } = renderWorkspace("/c/session-b");

    await waitFor(() => expect(chatStoreMocks.loadInBackground).toHaveBeenCalledWith("session-a"));
    expect(chatStoreMocks.loadInBackground).not.toHaveBeenCalledWith("session-b");

    // Both pane conversations are pinned against stream-slot eviction while
    // visible, and released once the workspace unmounts.
    expect(pinSpy).toHaveBeenCalledWith("session-a");
    expect(pinSpy).toHaveBeenCalledWith("session-b");
    unmount();
    expect(unpinSpy).toHaveBeenCalledWith("session-a");
    expect(unpinSpy).toHaveBeenCalledWith("session-b");
    pinSpy.mockRestore();
    unpinSpy.mockRestore();
  });

  it("keeps the landing and ordinary single-session routes unsplit", async () => {
    const { unmount } = renderWorkspace("/");
    expect(screen.getByTestId("chat-landing")).toBeInTheDocument();

    unmount();
    renderWorkspace("/c/session-c");

    await waitFor(() => expect(screen.getByTestId("chat-session-c")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Close pane/ })).toBeNull();
  });
});
