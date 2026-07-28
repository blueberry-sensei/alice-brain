export type WorkspaceSection = "search" | "knowledge";

export interface WorkspaceSectionDefinition {
  id: WorkspaceSection;
  href: string;
  shortcut?: string;
}

/**
 * The single entry point for the workbench capabilities. normal and mini only change the presentation; neither keeps its own menu.
 */
const WORKSPACE_SECTION_DEFINITIONS: readonly WorkspaceSectionDefinition[] = [
  { id: "search", href: "/search", shortcut: "⌘K" },
  { id: "knowledge", href: "/knowledge" },
];

export const WORKSPACE_SECTIONS = WORKSPACE_SECTION_DEFINITIONS;

export function isWorkspaceSection(value: unknown): value is WorkspaceSection {
  return value === "search" || value === "knowledge";
}

export function workspaceSectionFromPathname(pathname: string): WorkspaceSection | null {
  if (pathname === "/search" || pathname.startsWith("/search/")) return "search";
  if (pathname === "/knowledge" || pathname.startsWith("/knowledge/")) return "knowledge";
  return null;
}

export function workspaceSectionDefinition(section: WorkspaceSection) {
  return WORKSPACE_SECTION_DEFINITIONS.find((item) => item.id === section)!;
}
