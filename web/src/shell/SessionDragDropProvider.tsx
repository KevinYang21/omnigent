import {
  DndContext,
  DragOverlay,
  type DragEndEvent,
  type DragStartEvent,
  MouseSensor,
  TouchSensor,
  pointerWithin,
  useSensor,
  useSensors,
} from "@dnd-kit/core";
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useNavigate } from "@/lib/routing";
import { useWorkspaceLayoutStore, type WorkspaceDropEdge } from "@/store/workspaceLayout";
import type { SidebarDropTarget } from "./sidebarNav";

export interface SessionDragState {
  id: string;
  label: string;
  project: string | null;
  isPinned: boolean;
}

export interface WorkspacePaneDropTarget {
  type: "workspace-pane";
  paneId: string;
  edge: WorkspaceDropEdge;
}

type SidebarDropHandler = (drag: SessionDragState, target: SidebarDropTarget) => void;

interface SessionDragDropContextValue {
  activeDrag: SessionDragState | null;
  registerSidebarDropHandler: (handler: SidebarDropHandler) => () => void;
}

const SessionDragDropContext = createContext<SessionDragDropContextValue>({
  activeDrag: null,
  registerSidebarDropHandler: () => () => {},
});

export function useSessionDragDrop(): SessionDragDropContextValue {
  return useContext(SessionDragDropContext);
}

function dragStateFromEvent(event: DragStartEvent): SessionDragState {
  const data = event.active.data.current as
    { label?: string; project?: string | null; isPinned?: boolean } | undefined;
  return {
    id: String(event.active.id),
    label: data?.label ?? String(event.active.id),
    project: data?.project ?? null,
    isPinned: data?.isPinned ?? false,
  };
}

function isWorkspaceDropTarget(value: unknown): value is WorkspacePaneDropTarget {
  if (!value || typeof value !== "object") return false;
  const target = value as Partial<WorkspacePaneDropTarget>;
  return (
    target.type === "workspace-pane" &&
    typeof target.paneId === "string" &&
    (target.edge === "left" ||
      target.edge === "right" ||
      target.edge === "top" ||
      target.edge === "bottom")
  );
}

export function SessionDragDropProvider({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const [activeDrag, setActiveDrag] = useState<SessionDragState | null>(null);
  const activeDragRef = useRef<SessionDragState | null>(null);
  const sidebarDropHandlerRef = useRef<SidebarDropHandler | null>(null);
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 5 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
  );

  const registerSidebarDropHandler = useCallback((handler: SidebarDropHandler) => {
    sidebarDropHandlerRef.current = handler;
    return () => {
      if (sidebarDropHandlerRef.current === handler) sidebarDropHandlerRef.current = null;
    };
  }, []);

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const drag = dragStateFromEvent(event);
    activeDragRef.current = drag;
    setActiveDrag(drag);
  }, []);

  const clearDrag = useCallback(() => {
    activeDragRef.current = null;
    setActiveDrag(null);
  }, []);

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const drag = activeDragRef.current;
      clearDrag();
      if (!drag) return;

      const target = event.over?.data.current;
      if (isWorkspaceDropTarget(target)) {
        useWorkspaceLayoutStore.getState().splitPane(target.paneId, drag.id, target.edge);
        navigate(`/c/${drag.id}`);
        return;
      }
      sidebarDropHandlerRef.current?.(drag, (target as SidebarDropTarget | undefined) ?? null);
    },
    [clearDrag, navigate],
  );

  const value = useMemo(
    () => ({ activeDrag, registerSidebarDropHandler }),
    [activeDrag, registerSidebarDropHandler],
  );

  return (
    <SessionDragDropContext.Provider value={value}>
      <DndContext
        sensors={sensors}
        collisionDetection={pointerWithin}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
        onDragCancel={clearDrag}
      >
        {children}
        <DragOverlay dropAnimation={null}>
          {activeDrag ? (
            <div className="pointer-events-none max-w-[16rem] truncate rounded-md border bg-card-solid px-3 py-2 text-ui shadow-tooltip">
              {activeDrag.label}
            </div>
          ) : null}
        </DragOverlay>
      </DndContext>
    </SessionDragDropContext.Provider>
  );
}
