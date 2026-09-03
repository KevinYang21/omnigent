import { useDroppable } from "@dnd-kit/core";
import { X } from "lucide-react";
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useNavigate, useParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { ChatStoreScopeProvider, useChatStore } from "@/store/chatStore";
import { conversationRegistry } from "@/store/conversationRegistry";
import {
  findWorkspaceLeaf,
  useWorkspaceLayoutStore,
  type WorkspaceDropEdge,
  type WorkspaceLeaf,
  type WorkspaceNode,
  type WorkspaceSplit,
} from "@/store/workspaceLayout";
import { useConversations } from "@/hooks/useConversations";
import { useSession } from "@/hooks/useSession";
import { useSessionDragDrop } from "@/shell/SessionDragDropProvider";
import { UNTITLED_CONVERSATION_LABEL } from "@/shell/sidebarNav";
import { ChatPage } from "./ChatPage";

export function WorkspacePage() {
  const { conversationId } = useParams<{ conversationId: string }>();
  const root = useWorkspaceLayoutStore((state) => state.root);
  const focusedPaneId = useWorkspaceLayoutStore((state) => state.focusedPaneId);
  const selectSession = useWorkspaceLayoutStore((state) => state.selectSession);
  const leafCount = useMemo(() => countLeaves(root), [root]);

  useLayoutEffect(() => {
    if (conversationId) selectSession(conversationId);
  }, [conversationId, selectSession]);

  if (!conversationId) return <ChatPage />;

  // ChatHeader is a floating h-14/md:h-12 overlay; in split mode the pane
  // title strips must start below it instead of rendering underneath.
  return (
    <div
      className={cn(
        "flex min-h-0 min-w-0 flex-1 overflow-hidden bg-background",
        leafCount > 1 && "pt-14 md:pt-12",
      )}
    >
      <WorkspaceNodeView node={root} focusedPaneId={focusedPaneId} leafCount={leafCount} />
    </div>
  );
}

function WorkspaceNodeView({
  node,
  focusedPaneId,
  leafCount,
}: {
  node: WorkspaceNode;
  focusedPaneId: string;
  leafCount: number;
}) {
  if (node.kind === "leaf") {
    return (
      <WorkspaceLeafView node={node} focused={node.id === focusedPaneId} leafCount={leafCount} />
    );
  }
  return <WorkspaceSplitView node={node} focusedPaneId={focusedPaneId} leafCount={leafCount} />;
}

function WorkspaceSplitView({
  node,
  focusedPaneId,
  leafCount,
}: {
  node: WorkspaceSplit;
  focusedPaneId: string;
  leafCount: number;
}) {
  const resizeSplit = useWorkspaceLayoutStore((state) => state.resizeSplit);
  const containerRef = useRef<HTMLDivElement>(null);
  const horizontal = node.direction === "horizontal";

  const startResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const container = containerRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    const update = (clientX: number, clientY: number) => {
      const position = horizontal ? clientX - rect.left : clientY - rect.top;
      const extent = horizontal ? rect.width : rect.height;
      if (extent > 0) resizeSplit(node.id, (position / extent) * 100);
    };
    const onPointerMove = (moveEvent: PointerEvent) => update(moveEvent.clientX, moveEvent.clientY);
    const onPointerUp = () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = horizontal ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp, { once: true });
  };

  const resizeByKeyboard = (delta: number) => resizeSplit(node.id, node.sizes[0] + delta);

  return (
    <div
      ref={containerRef}
      className={cn("flex min-h-0 min-w-0 flex-1", horizontal ? "flex-row" : "flex-col")}
      data-workspace-split={node.direction}
    >
      <div className="flex min-h-0 min-w-0" style={{ flexBasis: `${node.sizes[0]}%` }}>
        <WorkspaceNodeView
          node={node.children[0]}
          focusedPaneId={focusedPaneId}
          leafCount={leafCount}
        />
      </div>
      <div
        role="separator"
        aria-label={`Resize ${node.direction} split`}
        aria-orientation={horizontal ? "vertical" : "horizontal"}
        tabIndex={0}
        onPointerDown={startResize}
        onKeyDown={(event) => {
          if (event.key === (horizontal ? "ArrowLeft" : "ArrowUp")) {
            event.preventDefault();
            resizeByKeyboard(-5);
          }
          if (event.key === (horizontal ? "ArrowRight" : "ArrowDown")) {
            event.preventDefault();
            resizeByKeyboard(5);
          }
        }}
        className={cn(
          "group relative z-20 shrink-0 bg-transparent focus:outline-none",
          horizontal ? "w-1.5 cursor-col-resize" : "h-1.5 cursor-row-resize",
        )}
      >
        <div
          className={cn(
            "absolute bg-border transition-colors group-hover:bg-primary/60 group-focus-visible:bg-primary/70",
            horizontal
              ? "inset-y-0 left-1/2 w-px -translate-x-1/2"
              : "inset-x-0 top-1/2 h-px -translate-y-1/2",
          )}
        />
      </div>
      <div className="flex min-h-0 min-w-0" style={{ flexBasis: `${node.sizes[1]}%` }}>
        <WorkspaceNodeView
          node={node.children[1]}
          focusedPaneId={focusedPaneId}
          leafCount={leafCount}
        />
      </div>
    </div>
  );
}

function WorkspaceLeafView({
  node,
  focused,
  leafCount,
}: {
  node: WorkspaceLeaf;
  focused: boolean;
  leafCount: number;
}) {
  const navigate = useNavigate();
  const focusPane = useWorkspaceLayoutStore((state) => state.focusPane);
  const closePane = useWorkspaceLayoutStore((state) => state.closePane);
  const sessionId = node.sessionId;

  // Non-focused panes have no route driving switchTo, so hydrate their
  // conversation in the background — after a reload only the focused pane's
  // conversation is live, and an unbound pane must not paint the focused
  // pane's transcript.
  useEffect(() => {
    if (!sessionId || focused) return;
    void useChatStore.getState().loadInBackground(sessionId);
  }, [sessionId, focused]);

  // A visible pane's conversation must never lose its stream slot: eviction
  // would drop the entry and the pane would fall back to an empty transcript.
  useEffect(() => {
    if (!sessionId) return;
    conversationRegistry.pin(sessionId);
    return () => conversationRegistry.unpin(sessionId);
  }, [sessionId]);

  const focus = () => {
    if (!sessionId || focused) return;
    focusPane(node.id);
    void useChatStore.getState().switchTo(sessionId);
    navigate(`/c/${sessionId}`);
  };

  const close = () => {
    closePane(node.id);
    const state = useWorkspaceLayoutStore.getState();
    const nextSessionId = findWorkspaceLeaf(state.root, state.focusedPaneId)?.sessionId;
    if (nextSessionId) {
      void useChatStore.getState().switchTo(nextSessionId);
      navigate(`/c/${nextSessionId}`);
    }
  };

  return (
    <div
      className={cn(
        "relative flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background",
        leafCount > 1 &&
          focused &&
          "shadow-[inset_0_0_0_1px_color-mix(in_srgb,var(--primary)_55%,transparent)]",
      )}
      data-testid={`workspace-pane-${node.id}`}
      data-workspace-pane-id={node.id}
      data-focused={String(focused)}
      onPointerDownCapture={focus}
    >
      {leafCount > 1 && sessionId ? (
        <WorkspacePaneHeader
          paneId={node.id}
          sessionId={sessionId}
          focused={focused}
          onClose={close}
        />
      ) : null}
      {sessionId ? (
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          <ChatStoreScopeProvider conversationId={sessionId}>
            <ChatPage conversationId={sessionId} active={focused} />
          </ChatStoreScopeProvider>
        </div>
      ) : null}
      <WorkspaceDropZones paneId={node.id} />
    </div>
  );
}

function WorkspacePaneHeader({
  paneId,
  sessionId,
  focused,
  onClose,
}: {
  paneId: string;
  sessionId: string;
  focused: boolean;
  onClose: () => void;
}) {
  const { data: conversationsData } = useConversations("", true);
  const { session } = useSession(sessionId);
  const title = useMemo(() => {
    const listed = conversationsData?.pages
      .flatMap((page) => page.data)
      .find((conversation) => conversation.id === sessionId);
    return listed?.title || session?.title || UNTITLED_CONVERSATION_LABEL;
  }, [conversationsData, session, sessionId]);

  return (
    <div className="flex h-8 shrink-0 items-center gap-1 border-b bg-muted/30 pr-1.5 pl-3">
      <span
        data-testid={`pane-title-${paneId}`}
        className={cn(
          "min-w-0 flex-1 truncate text-xs",
          focused ? "text-foreground" : "text-muted-foreground",
        )}
      >
        {title}
      </span>
      <button
        type="button"
        aria-label={`Close pane ${sessionId}`}
        onClick={(event) => {
          event.stopPropagation();
          onClose();
        }}
        className="flex size-5 shrink-0 items-center justify-center rounded text-muted-foreground/70 transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <X className="size-3" />
      </button>
    </div>
  );
}

/* SP2K-style drop targets: a plus/cross of five invisible zones armed only
 * while a session drag is active. Nothing shows until the pointer enters a
 * zone, which then lights up with a subtle accent highlight — no icons. */
function WorkspaceDropZones({ paneId }: { paneId: string }) {
  const { activeDrag } = useSessionDragDrop();
  const armed = activeDrag !== null;
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 z-[80] flex flex-col">
      <WorkspaceDropZone paneId={paneId} edge="top" armed={armed} sizing="h-[18%] w-full" />
      <div className="flex min-h-0 flex-1">
        <WorkspaceDropZone paneId={paneId} edge="left" armed={armed} sizing="h-full w-[25%]" />
        <WorkspaceDropZone paneId={paneId} edge="center" armed={armed} sizing="h-full flex-1" />
        <WorkspaceDropZone paneId={paneId} edge="right" armed={armed} sizing="h-full w-[25%]" />
      </div>
      <WorkspaceDropZone paneId={paneId} edge="bottom" armed={armed} sizing="h-[18%] w-full" />
    </div>
  );
}

function WorkspaceDropZone({
  paneId,
  edge,
  armed,
  sizing,
}: {
  paneId: string;
  edge: WorkspaceDropEdge | "center";
  armed: boolean;
  sizing: string;
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `workspace-pane:${paneId}:${edge}`,
    data: { type: "workspace-pane", paneId, edge },
    disabled: !armed,
  });

  return (
    <div
      ref={setNodeRef}
      data-testid={`drop-zone-${paneId}-${edge}`}
      className={cn("relative", sizing)}
    >
      <div
        className={cn(
          "absolute inset-1.5 rounded-md border-[1.5px] border-transparent opacity-0 transition-opacity duration-100",
          armed && isOver && "border-primary/70 bg-primary/15 opacity-100",
        )}
      />
    </div>
  );
}

function countLeaves(node: WorkspaceNode): number {
  return node.kind === "leaf" ? 1 : countLeaves(node.children[0]) + countLeaves(node.children[1]);
}
