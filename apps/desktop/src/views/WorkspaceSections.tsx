import type { TabRef } from "./Tabs";

export type WorkspaceSection = "workspaces" | "runs" | "git";

const PAGES: Record<WorkspaceSection, [string, TabRef]> = {
  workspaces: ["Workspaces", { kind: "workspaces", title: "Workspaces" }],
  runs: ["Runs", { kind: "runs", ref: "workspace", title: "Workspace runs" }],
  git: ["Git credentials", { kind: "git-credentials", title: "Git credentials" }],
};

/** The Workspaces page's tabs: your workspaces, the runs started in them, and the Git credentials they use. */
export function WorkspaceSections({ active, openTab }: { active: WorkspaceSection; openTab: (tab: TabRef) => void }) {
  return <nav className="object-tabs" aria-label="Workspace sections">
    {(Object.keys(PAGES) as WorkspaceSection[]).map((id) => <button key={id} aria-current={active === id ? "page" : undefined}
      className={active === id ? "active" : ""} onClick={() => active !== id && openTab(PAGES[id][1])}>{PAGES[id][0]}</button>)}
  </nav>;
}
