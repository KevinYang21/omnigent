import { useEffect, useState } from "react";
import { CheckIcon, ChevronUpIcon, PlusIcon, ServerIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  getServerPicker,
  openServerSetup,
  switchServer,
  type ServerPickerInfo,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";
import { SIDEBAR_ROW } from "./sidebarStyles";

/** Compact display label that keeps path-based deployments distinguishable. */
function serverLabel(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/$/, "");
    return `${parsed.host}${path}`;
  } catch {
    return url;
  }
}

/** Browser-canonical server identity, retaining path and query. */
function serverKey(url: string): string {
  try {
    const parsed = new URL(url);
    parsed.hash = "";
    if (parsed.pathname.length > 1) parsed.pathname = parsed.pathname.replace(/\/$/, "");
    return parsed.href;
  } catch {
    return `raw:${url}`;
  }
}

/**
 * The server picker for every native shell (Electron, iOS, Android), pinned to
 * the sidebar's bottom.
 *
 * A sidebar row (server glyph + current host + an upward chevron) that opens a
 * menu of organization-provided and recently-connected servers — selecting one
 * re-points the whole surface via the shell; setup opens from the final item.
 *
 * This deliberately lives at the bottom of the sidebar rather than in any
 * shell's own chrome. On desktop the chat header already contests the freed
 * title-bar strip; on iOS/Android a floating top pill fought the header band
 * and needed inset/visibility choreography. Docking the picker here gives all
 * three shells one picker with none of that.
 *
 * Renders nothing until the shell confirms this page is a connected server
 * (getServerPicker resolves non-null) — so it's absent in plain browsers, under
 * shells too old for the picker bridge, and on foreign pages. That single check
 * is the whole gate: no platform sniffing, matching how the rest of
 * nativeBridge degrades (one bundle, many runtimes, decided at runtime).
 */
export function SidebarServerPicker() {
  const [info, setInfo] = useState<ServerPickerInfo | null>(null);

  const refresh = () => {
    void getServerPicker().then((result) => {
      if (result) setInfo(result);
    });
  };

  useEffect(() => {
    let cancelled = false;
    void getServerPicker().then((result) => {
      if (!cancelled) setInfo(result);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!info) return null;

  const currentUrl = info.currentServerUrl ?? info.currentOrigin;
  const currentKey = serverKey(currentUrl);
  const matchesCurrent =
    info.currentServerUrl === undefined
      ? (url: string) => {
          try {
            return new URL(url).origin === info.currentOrigin;
          } catch {
            return false;
          }
        }
      : (url: string) => serverKey(url) === currentKey;
  const managed = Array.isArray(info.managedServers) ? info.managedServers : [];
  const managedKeys = new Set(managed.map(serverKey));
  const currentIsManaged = managed.some(matchesCurrent);
  // The current server leads its section even when settings were edited out
  // from under us. Managed servers are not repeated under Recents.
  const recentOthers = info.recentServers.filter((url) => {
    const key = serverKey(url);
    return !matchesCurrent(url) && !managedKeys.has(key);
  });
  const currentLabel = serverLabel(currentUrl);

  return (
    // shrink-0 keeps the row at its natural height so the scrolling session
    // list above (flex-1) gives up space instead of squashing it.
    <div className="shrink-0 px-2 pt-1 pb-2" data-testid="sidebar-server-picker-row">
      <DropdownMenu onOpenChange={(open) => open && refresh()}>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            // Same shared row construct as New session / Inbox / Settings, so
            // the icon lands on the sidebar's icon column and the label on its
            // label column.
            className={cn(
              SIDEBAR_ROW,
              "w-full justify-start border-0 font-normal",
              // Touch drawers need a 44px target while desktop stays compact.
              "min-h-11 md:min-h-0",
              "text-muted-foreground",
              "hover:bg-muted hover:text-foreground dark:hover:bg-muted/50",
              "data-[state=open]:bg-muted data-[state=open]:text-foreground",
            )}
            aria-label={`Server: ${currentLabel}. Switch server`}
            data-testid="sidebar-server-picker"
          >
            <ServerIcon className="ui-icon text-muted-foreground" />
            <span className="truncate">{currentLabel}</span>
            {/* Points up: the menu opens upward from the sidebar's bottom. */}
            <ChevronUpIcon className="ui-icon ml-auto shrink-0 text-muted-foreground" />
          </Button>
        </DropdownMenuTrigger>
        {/* side="top" — the trigger sits at the bottom of the window, so the
            menu must grow upward rather than off-screen. */}
        <DropdownMenuContent side="top" align="start" collisionPadding={8} className="min-w-56">
          {managed.length > 0 ? (
            <>
              <DropdownMenuLabel className="text-muted-foreground">
                Provided by your organization
              </DropdownMenuLabel>
              {managed.map((url) => {
                const isCurrent = matchesCurrent(url);
                return (
                  <DropdownMenuItem
                    key={url}
                    disabled={isCurrent}
                    className={cn("gap-2 min-h-11 md:min-h-0", isCurrent && "opacity-100")}
                    onSelect={isCurrent ? undefined : () => void switchServer(url)}
                  >
                    {isCurrent ? (
                      <CheckIcon className="size-4 shrink-0" />
                    ) : (
                      <span className="size-4 shrink-0" aria-hidden="true" />
                    )}
                    <span className={cn("truncate", isCurrent && "font-medium")}>
                      {serverLabel(url)}
                    </span>
                  </DropdownMenuItem>
                );
              })}
            </>
          ) : null}
          {!currentIsManaged || recentOthers.length > 0 ? (
            <>
              {managed.length > 0 ? <DropdownMenuSeparator /> : null}
              <DropdownMenuLabel className="text-muted-foreground">Recents</DropdownMenuLabel>
              {!currentIsManaged ? (
                <DropdownMenuItem disabled className="gap-2 min-h-11 opacity-100 md:min-h-0">
                  <CheckIcon className="size-4 shrink-0" />
                  <span className="truncate font-medium">{currentLabel}</span>
                </DropdownMenuItem>
              ) : null}
              {recentOthers.map((url) => (
                <DropdownMenuItem
                  key={url}
                  className="gap-2 min-h-11 md:min-h-0"
                  onSelect={() => void switchServer(url)}
                >
                  <span className="size-4 shrink-0" aria-hidden="true" />
                  <span className="truncate">{serverLabel(url)}</span>
                </DropdownMenuItem>
              ))}
            </>
          ) : null}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="gap-2 min-h-11 md:min-h-0"
            onSelect={() => openServerSetup()}
          >
            <PlusIcon className="size-4 shrink-0" />
            Connect to new server…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
