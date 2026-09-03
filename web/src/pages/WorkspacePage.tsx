import { useDroppable } from "@dnd-kit/core";
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, X } from "lucide-react";
import { useLayoutEffect, useMemo, useRef, type PointerEvent as ReactPointerEvent } from "react";
import { useNavigate, useParams } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { ChatStoreScopeProvider, useChatStore } from "@/store/chatStore";
import {
  findWorkspaceLeaf,
  useWorkspaceLayoutStore,
  type WorkspaceDropEdge,
  type WorkspaceLeaf,
  type WorkspaceNode,
  type WorkspaceSplit,
} from "@/store/workspaceLayout";
import { useSessionDragDrop } from "@/shell/SessionDragDropProvider";
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

  return (
    <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden bg-background">
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
        "relative flex min-h-0 min-w-0 flex-1 overflow-hidden bg-background",
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
        <button
          type="button"
          aria-label={`Close pane ${sessionId}`}
          onClick={(event) => {
            event.stopPropagation();
            close();
          }}
          className="absolute top-2 left-9 z-[70] flex size-5 items-center justify-center rounded text-muted-foreground/70 transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          <X className="size-3" />
        </button>
      ) : null}
      {sessionId ? (
        <ChatStoreScopeProvider conversationId={sessionId}>
          <ChatPage conversationId={sessionId} active={focused} />
        </ChatStoreScopeProvider>
      ) : null}
      <WorkspaceDropZones paneId={node.id} />
    </div>
  );
}

function WorkspaceDropZones({ paneId }: { paneId: string }) {
  const { activeDrag } = useSessionDragDrop();
  return (
    <>
      <WorkspaceDropZone paneId={paneId} edge="left" active={activeDrag !== null} />
      <WorkspaceDropZone paneId={paneId} edge="right" active={activeDrag !== null} />
      <WorkspaceDropZone paneId={paneId} edge="top" active={activeDrag !== null} />
      <WorkspaceDropZone paneId={paneId} edge="bottom" active={activeDrag !== null} />
    </>
  );
}

const DROP_ZONE_ICON = {
  left: ArrowLeft,
  right: ArrowRight,
  top: ArrowUp,
  bottom: ArrowDown,
} satisfies Record<WorkspaceDropEdge, typeof ArrowLeft>;

function WorkspaceDropZone({
  paneId,
  edge,
  active,
}: {
  paneId: string;
  edge: WorkspaceDropEdge;
  active: boolean;
}) {
  const { setNodeRef, isOver } = useDroppable({
    id: `workspace-pane:${paneId}:${edge}`,
    data: { type: "workspace-pane", paneId, edge },
    disabled: !active,
  });
  const Icon = DROP_ZONE_ICON[edge];

  return (
    <div
      ref={setNodeRef}
      aria-label={`Split session ${edge}`}
      className={cn(
        "pointer-events-none absolute z-[80] flex items-center justify-center rounded-md border border-transparent opacity-0 transition-[opacity,background-color,border-color] duration-100",
        edge === "left" && "inset-y-3 left-3 w-[24%]",
        edge === "right" && "inset-y-3 right-3 w-[24%]",
        edge === "top" && "inset-x-[27%] top-3 h-[24%]",
        edge === "bottom" && "inset-x-[27%] bottom-3 h-[24%]",
        active && "pointer-events-auto opacity-100 bg-background/65 backdrop-blur-[2px]",
        isOver && "border-primary bg-primary/15 text-primary shadow-sm",
      )}
    >
      <Icon className="size-5" />
    </div>
  );
}

function countLeaves(node: WorkspaceNode): number {
  return node.kind === "leaf" ? 1 : countLeaves(node.children[0]) + countLeaves(node.children[1]);
}
