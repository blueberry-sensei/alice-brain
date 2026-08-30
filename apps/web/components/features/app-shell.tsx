"use client";

import * as React from "react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { usePathname, useRouter } from "next/navigation";
import { Grip, Settings2 } from "lucide-react";
import { motion } from "motion/react";
import { toast } from "sonner";

import { api, ApiError } from "@/lib/api";
import { clearToken, getToken, setToken } from "@/lib/auth";
import {
  APP_INITIALIZATION_DEFAULTS,
  persistAppMode,
  readInitialAppState,
  rememberThemeBeforeExplore,
  resolveThemePreference,
  restoreThemeAfterExplore,
  type AppMode,
  type ThemePreference,
} from "@/lib/app-initialization";
import { DEFAULT_AGENT_AVATAR } from "@/lib/branding";
import { DEFAULT_TIME_ZONE, detectSystemTimeZone } from "@/lib/format";
import {
  DEFAULT_SEARCH_STRATEGY,
  isSearchStrategy,
} from "@/lib/retrieval-config";
import {
  settingsTabHref,
  type SettingsTab,
} from "@/lib/settings-config";
import type { Agent, Capabilities, Thread, User } from "@/lib/types";
import {
  type WorkspaceSection,
  workspaceSectionFromPathname,
} from "@/lib/workspace";
import {
  UNIVERSE_DETAIL_EVENT,
  dispatchUniverseReset,
} from "@/lib/universe-events";
import { cn } from "@/lib/utils";
import {
  DEFAULT_WINDOW_MODE,
  DEFAULT_WINDOW_SIZE,
  clampWindowSize,
  persistWindowMode,
  persistWindowSize,
  readWindowMode,
  readWindowSize,
  resolveWindowScalingEnabled,
  type WindowMode,
  type WindowSize,
} from "@/lib/window-layout";
import { AppSidebar } from "@/components/features/app-sidebar";
import {
  DetailPanelMain,
  DetailPanelOutlet,
  DetailPanelProvider,
  DetailPanelSheet,
  useDetailPanel,
  useIsLgUp,
} from "@/components/features/detail-panel";
import { KnowledgeProvider } from "@/components/features/knowledge-provider";
import { AgentAvatar } from "@/components/features/agent-avatar";
import { SearchProvider } from "@/components/features/search/search-provider";
import { SpaceBackdrop } from "@/components/features/space-backdrop";
import { SiteHeader } from "@/components/features/site-header";
import { ThemeToggle } from "@/components/features/theme-toggle";
import { UniverseViewSettingsDrawer } from "@/components/features/universe-view-settings-drawer";
import { Button } from "@/components/ui/button";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar";

const KnowledgeUniverse = dynamic(
  () =>
    import("@/components/features/knowledge-universe").then(
      (module) => module.KnowledgeUniverse,
    ),
  { ssr: false },
);

export type { AppMode } from "@/lib/app-initialization";

const WINDOW_SCALING_ENABLED = resolveWindowScalingEnabled(
  process.env.NEXT_PUBLIC_ENABLE_WINDOW_SCALING,
);

function currentViewportSize(): WindowSize {
  return { width: window.innerWidth, height: window.innerHeight };
}

interface AppCtx {
  user: User | null;
  capabilities: Capabilities | null;
  /** The default agent (the client's main conversation entry) */
  agent: Agent | null;
  replaceAgent: (agent: Agent) => void;
  threads: Thread[];
  hasMoreThreads: boolean;
  threadsExpanded: boolean;
  loadingMoreThreads: boolean;
  refreshThreads: () => Promise<void>;
  loadMoreThreads: () => Promise<void>;
  collapseThreads: () => void;
  windowScalingEnabled: boolean;
  windowMode: WindowMode;
  toggleWindowMode: () => void;
  appMode: AppMode;
  workspaceSection: WorkspaceSection;
  enterExploreMode: (section?: WorkspaceSection) => void;
  exitExploreMode: () => void;
  openSettings: (tab?: SettingsTab, section?: string) => void;
  refreshCapabilities: () => Promise<void>;
  timezone: string;
  updateTimezone: (timezone: string) => Promise<void>;
}

const AppContext = React.createContext<AppCtx>({
  user: null,
  capabilities: null,
  agent: null,
  replaceAgent: () => {},
  threads: [],
  hasMoreThreads: false,
  threadsExpanded: false,
  loadingMoreThreads: false,
  refreshThreads: async () => {},
  loadMoreThreads: async () => {},
  collapseThreads: () => {},
  windowScalingEnabled: WINDOW_SCALING_ENABLED,
  windowMode: WINDOW_SCALING_ENABLED ? DEFAULT_WINDOW_MODE : "full",
  toggleWindowMode: () => {},
  appMode: APP_INITIALIZATION_DEFAULTS.appMode,
  workspaceSection: APP_INITIALIZATION_DEFAULTS.workspaceSection,
  enterExploreMode: () => {},
  exitExploreMode: () => {},
  openSettings: () => {},
  refreshCapabilities: async () => {},
  timezone: DEFAULT_TIME_ZONE,
  updateTimezone: async () => {},
});

export function useApp() {
  return React.useContext(AppContext);
}

function FullLoader() {
  const t = useTranslations("AppShell");
  return (
    <div className="bg-space-field grid h-screen place-items-center">
      <SpaceBackdrop />
      <div
        className="relative z-10 flex flex-col items-center gap-2.5"
        role="status"
        aria-live="polite"
      >
        <AgentAvatar face={DEFAULT_AGENT_AVATAR} size="lg" />
        <span className="text-sm text-muted-foreground">{t("loading")}</span>
      </div>
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const t = useTranslations("AppShell");
  const router = useRouter();
  const pathname = usePathname();
  const { theme, resolvedTheme, setTheme } = useTheme();
  const [user, setUser] = React.useState<User | null>(null);
  const [capabilities, setCapabilities] = React.useState<Capabilities | null>(null);
  const [appMode, setAppMode] = React.useState<AppMode>(
    APP_INITIALIZATION_DEFAULTS.appMode,
  );
  const [windowMode, setWindowMode] = React.useState<WindowMode>(
    WINDOW_SCALING_ENABLED ? DEFAULT_WINDOW_MODE : "full",
  );
  const [windowSize, setWindowSize] = React.useState<WindowSize>(
    DEFAULT_WINDOW_SIZE,
  );
  const [isDesktop, setIsDesktop] = React.useState(true);
  const [workspaceSection, setWorkspaceSection] = React.useState<WorkspaceSection>(
    APP_INITIALIZATION_DEFAULTS.workspaceSection,
  );
  const [sidebarOpen, setSidebarOpen] = React.useState(true);
  const [loading, setLoading] = React.useState(true);
  const [timezone, setTimezone] = React.useState(DEFAULT_TIME_ZONE);
  const sidebarOpenRef = React.useRef(true);
  const restoreSidebarOpenRef = React.useRef<boolean | null>(null);
  const previousThemeModeRef = React.useRef<AppMode | null>(null);
  const themeBeforeExploreRef = React.useRef<ThemePreference | null>(null);
  const currentThemeRef = React.useRef<ThemePreference>(
    resolveThemePreference(theme, resolvedTheme),
  );
  currentThemeRef.current = resolveThemePreference(theme, resolvedTheme);

  // Restore the mode, the workspace, and - when the web build allows it - the simulated window preference on first paint.
  React.useEffect(() => {
    const initial = readInitialAppState(window.localStorage);
    setAppMode(initial.mode);
    setWorkspaceSection(initial.section);
    if (WINDOW_SCALING_ENABLED) {
      setWindowMode(readWindowMode(window.localStorage));
      setWindowSize(readWindowSize(window.localStorage, currentViewportSize()));
    }
  }, []);

  React.useEffect(() => {
    if (!WINDOW_SCALING_ENABLED) {
      setIsDesktop(false);
      return;
    }
    const query = window.matchMedia("(min-width: 768px)");
    const update = () => setIsDesktop(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  React.useEffect(() => {
    if (loading) return;
    const previousMode = previousThemeModeRef.current;
    previousThemeModeRef.current = appMode;

    if (appMode === "explore") {
      if (previousMode === "explore") return;
      const beforeExplore = rememberThemeBeforeExplore(
        window.localStorage,
        currentThemeRef.current,
      );
      themeBeforeExploreRef.current = beforeExplore;
      currentThemeRef.current = "dark";
      setTheme("dark");
      return;
    }

    if (previousMode !== "explore") return;
    const beforeExplore = restoreThemeAfterExplore(window.localStorage)
      ?? themeBeforeExploreRef.current;
    themeBeforeExploreRef.current = null;
    if (!beforeExplore) return;
    currentThemeRef.current = beforeExplore;
    setTheme(beforeExplore);
  }, [appMode, loading, setTheme]);

  const enterExploreMode = React.useCallback((section?: WorkspaceSection) => {
    const nextSection = section
      ?? workspaceSectionFromPathname(pathname)
      ?? workspaceSection;
    if (appMode !== "explore") {
      // Entering from search/answer is a fresh visit to the universe home.
      // Context results remain in their panels; the graph starts at overview
      // and only enters a source after the user deliberately selects one.
      dispatchUniverseReset("explore-home");
    }
    setWorkspaceSection(nextSection);
    setAppMode("explore");
    persistAppMode(window.localStorage, "explore", nextSection);
    if (appMode !== "explore") {
      toast(t("exploreEntered"), {
        id: "explore-mode-entered",
        description: t("exploreEnteredDescription"),
        duration: 5000,
      });
    }
  }, [appMode, pathname, t, workspaceSection]);

  const exitExploreMode = React.useCallback(() => {
    toast.dismiss("explore-mode-entered");
    setAppMode("normal");
    persistAppMode(window.localStorage, "normal");
  }, []);

  const toggleWindowMode = React.useCallback(() => {
    if (!WINDOW_SCALING_ENABLED) return;
    setWindowMode((current) => {
      const next: WindowMode = current === "full" ? "window" : "full";
      persistWindowMode(window.localStorage, next);
      return next;
    });
  }, []);

  const openSettings = React.useCallback((tab: SettingsTab = "account", section?: string) => {
    setAppMode("normal");
    persistAppMode(window.localStorage, "normal");
    router.push(settingsTabHref(tab, section));
  }, [router]);

  React.useEffect(() => {
    // A node click opens a detail preview inside the current exploration; it
    // must not silently switch the workspace to search/cumulative mode.
    const revealDetail = () => enterExploreMode();
    window.addEventListener(UNIVERSE_DETAIL_EVENT, revealDetail);
    return () => {
      window.removeEventListener(UNIVERSE_DETAIL_EVENT, revealDetail);
    };
  }, [enterExploreMode]);

  React.useEffect(() => {
    sidebarOpenRef.current = sidebarOpen;
  }, [sidebarOpen]);

  const windowed = WINDOW_SCALING_ENABLED
    && isDesktop
    && windowMode === "window";

  React.useEffect(() => {
    if (!windowed) return;
    const onResize = () => {
      setWindowSize((current) => clampWindowSize(current, currentViewportSize()));
    };
    window.addEventListener("resize", onResize);
    onResize();
    return () => window.removeEventListener("resize", onResize);
  }, [windowed]);

  const startWindowResize = React.useCallback(
    (event: React.PointerEvent<HTMLButtonElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startY = event.clientY;
      const startSize = windowSize;
      let nextSize = startSize;
      const previousCursor = document.documentElement.style.cursor;
      const previousSelect = document.documentElement.style.userSelect;

      document.documentElement.style.cursor = "nwse-resize";
      document.documentElement.style.userSelect = "none";

      const onMove = (moveEvent: PointerEvent) => {
        nextSize = clampWindowSize(
          {
            width: startSize.width + moveEvent.clientX - startX,
            height: startSize.height + moveEvent.clientY - startY,
          },
          currentViewportSize(),
        );
        setWindowSize(nextSize);
      };

      const onUp = () => {
        document.documentElement.style.cursor = previousCursor;
        document.documentElement.style.userSelect = previousSelect;
        persistWindowSize(window.localStorage, nextSize);
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };

      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp, { once: true });
    },
    [windowSize],
  );

  React.useEffect(() => {
    const onDetailMaximized = (event: Event) => {
      const maximized = Boolean((event as CustomEvent<boolean>).detail);
      if (maximized) {
        if (restoreSidebarOpenRef.current === null) {
          restoreSidebarOpenRef.current = sidebarOpenRef.current;
        }
        setSidebarOpen(false);
        return;
      }
      if (restoreSidebarOpenRef.current !== null) {
        setSidebarOpen(restoreSidebarOpenRef.current);
        restoreSidebarOpenRef.current = null;
      }
    };
    window.addEventListener("sag:detail-maximized", onDetailMaximized);
    return () => window.removeEventListener("sag:detail-maximized", onDetailMaximized);
  }, []);

  const refreshCapabilities = React.useCallback(async () => {
    try {
      const next = await api.capabilities();
      setCapabilities(next);
      setTimezone(next.timezone || DEFAULT_TIME_ZONE);
    } catch {
      /* ignore */
    }
  }, []);

  const updateTimezone = React.useCallback(async (nextTimezone: string) => {
    const preferences = await api.saveSystemPreferences({ timezone: nextTimezone });
    setTimezone(preferences.timezone);
    setCapabilities((current) =>
      current ? { ...current, timezone: preferences.timezone } : current,
    );
  }, []);

  React.useEffect(() => {
    let alive = true;
    (async () => {
      try {
        // Brain là một người dùng cho mỗi project, chạy trên máy của chính người đó. Không có
        // gì để hỏi trước khi vào, nên phiên được mở ngay tại đây thay vì qua một trang nhập tên.
        if (!getToken()) {
          const session = await api.startSession();
          setToken(session.access_token);
        }
        const [u, c, preferences] = await Promise.all([
          api.me(),
          api.capabilities(),
          api.getSystemPreferences().catch(() => null),
        ]);
        let effectiveTimezone = preferences?.timezone_configured
          ? preferences.timezone
          : detectSystemTimeZone();
        if (preferences && !preferences.timezone_configured) {
          try {
            const saved = await api.saveSystemPreferences({ timezone: effectiveTimezone });
            effectiveTimezone = saved.timezone;
          } catch {
            // Browser detection remains useful for this session; UTC is its fallback.
          }
        }
        if (!alive) return;
        setUser(u);
        setCapabilities({ ...c, timezone: effectiveTimezone });
        setTimezone(effectiveTimezone);
      } catch (e) {
        // Token cũ không còn hợp lệ (đổi SAG_SECRET_KEY, xoá DB): bỏ nó đi và mở phiên mới
        // ở lần render sau. Không có trang đăng nhập để đẩy người dùng sang.
        if (e instanceof ApiError && e.status === 401) clearToken();
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);

  // The shortcut enters exploration mode directly and opens the matching compact workspace.
  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        enterExploreMode("search");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [enterExploreMode]);

  if (loading) return <FullLoader />;
  if (!user) return null;

  return (
    <AppContext.Provider
      value={{
        user,
        capabilities,
        agent: null,
        replaceAgent: () => {},
        threads: [],
        hasMoreThreads: false,
        threadsExpanded: false,
        loadingMoreThreads: false,
        refreshThreads: async () => {},
        loadMoreThreads: async () => {},
        collapseThreads: () => {},
        windowScalingEnabled: WINDOW_SCALING_ENABLED,
        windowMode,
        toggleWindowMode,
        appMode,
        workspaceSection,
        enterExploreMode,
        exitExploreMode,
        openSettings,
        refreshCapabilities,
        timezone,
        updateTimezone,
      }}
    >
      <SearchProvider
        defaultStrategy={
          isSearchStrategy(capabilities?.search_strategy)
            ? capabilities.search_strategy
            : DEFAULT_SEARCH_STRATEGY
        }
      >
        <KnowledgeProvider>
          <>
            <DetailPanelProvider>
              <div
                className={cn(
                  "bg-space-field relative grid h-svh min-h-0 overflow-hidden",
                  windowed && "place-items-center p-4",
                )}
              >
                <SpaceBackdrop variant={appMode === "explore" ? "universe" : "shell"} />
                <KnowledgeUniverse interactive={appMode === "explore"} />
                {appMode === "explore" && (
                  <motion.div
                    initial={{ opacity: 0, scale: 0.92, y: -4 }}
                    animate={{ opacity: 1, scale: 1, y: 0 }}
                    transition={{ duration: 0.18 }}
                    className="fixed right-4 top-3 z-[45] flex items-center gap-2"
                    data-explore-controls="true"
                  >
                    <ThemeToggle
                      className="size-8 border border-border/60 bg-background/80 shadow-soft backdrop-blur-md hover:border-amber-300/40 hover:bg-amber-300/10 hover:text-amber-200"
                    />
                    <UniverseViewSettingsDrawer
                      trigger={(
                        <Button
                          type="button"
                          variant="outline"
                          size="icon"
                          className="size-8 border-border/60 bg-background/80 shadow-soft backdrop-blur-md hover:border-[#7ea6ff]/35 hover:bg-[#4f86ff]/10 hover:text-[#c8d9ff]"
                          aria-label={t("graphSettings")}
                          title={t("graphSettings")}
                          data-universe-settings-trigger="true"
                        >
                          <Settings2 className="size-3.5" />
                        </Button>
                      )}
                    />
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-8 border-border/60 bg-background/80 px-3 text-xs font-medium shadow-soft backdrop-blur-md hover:border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
                      onClick={exitExploreMode}
                      aria-label={t("exitExplore")}
                      title={t("exitExplore")}
                    >
                      {t("exitExploreShort")}
                    </Button>
                  </motion.div>
                )}
                <motion.div
                  initial={false}
                  animate={
                    appMode === "normal"
                      ? { opacity: 1, scale: 1, y: 0 }
                      : { opacity: 0, scale: 0.9, y: 24 }
                  }
                  transition={{ type: "spring", stiffness: 340, damping: 30 }}
                  style={
                    windowed
                      ? { width: windowSize.width, height: windowSize.height }
                      : { width: "100%", height: "100svh" }
                  }
                  aria-hidden={appMode !== "normal"}
                  className={cn(
                    "relative z-10",
                    appMode !== "normal" && "invisible pointer-events-none",
                    windowed
                      ? "max-h-[calc(100svh-2rem)] max-w-[calc(100vw-2rem)] transform-gpu overflow-hidden rounded-xl border bg-background shadow-lift"
                      : "min-h-svh",
                  )}
                >
                  <SidebarProvider
                    open={sidebarOpen}
                    onOpenChange={setSidebarOpen}
                    className={cn(windowed ? "h-full min-h-full" : "h-svh min-h-svh")}
                  >
                    <AppSidebar contained={windowed} />
                    <SidebarInset className="min-w-0">
                      <SiteHeader />
                      <ContentArea>{children}</ContentArea>
                    </SidebarInset>
                  </SidebarProvider>
                  {windowed && (
                    <button
                      type="button"
                      aria-label={t("resizeWindow")}
                      title={t("resizeWindow")}
                      onPointerDown={startWindowResize}
                      className="absolute bottom-1.5 right-1.5 z-20 hidden size-7 cursor-nwse-resize items-center justify-center rounded-md text-muted-foreground/55 transition-colors hover:bg-muted hover:text-foreground md:flex"
                    >
                      <Grip className="size-3.5 rotate-45" />
                    </button>
                  )}
                </motion.div>
              </div>
            </DetailPanelProvider>
          </>
        </KnowledgeProvider>
      </SearchProvider>
    </AppContext.Provider>
  );
}

/** Content area: the official Resizable composition - the main area plus a draggable detail column (its width persisted through autoSaveId). */
function ContentArea({ children }: { children: React.ReactNode }) {
  const { target, maximized, panelRef } = useDetailPanel();
  const lg = useIsLgUp();
  React.useEffect(() => {
    window.dispatchEvent(new CustomEvent("sag:detail-maximized", { detail: maximized }));
  }, [maximized]);

  return (
    <>
      <ResizablePanelGroup
        direction="horizontal"
        className="min-h-0 flex-1"
        autoSaveId="sag:detail"
      >
        <ResizablePanel defaultSize={66} minSize={0}>
          <DetailPanelMain>{children}</DetailPanelMain>
        </ResizablePanel>
        {target && lg && (
          <>
            <ResizableHandle withHandle />
            <ResizablePanel ref={panelRef} defaultSize={34} minSize={24} maxSize={100} className="flex min-h-0 border-l">
              <DetailPanelOutlet />
            </ResizablePanel>
          </>
        )}
      </ResizablePanelGroup>
      {target && !lg && <DetailPanelSheet />}
    </>
  );
}
