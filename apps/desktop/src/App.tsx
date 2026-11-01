import { useEffect, useState } from "react";
import type { User } from "oidc-client-ts";
import { currentUser, getServer, onSessionExpired, resetManager, setServer, signIn, signOut, WEB_MODE } from "./auth";
import { useHub } from "./hub";
import { ObjectCacheProvider } from "./cache";
import {
  IconPin, IconHome, IconBook, IconAccount, IconAdmin, IconCatalog, IconClose, IconCompute, IconApi, IconPackage, IconData, IconStream, IconDeployed, IconPrivacy, IconSettings, IconWorkspaces,
} from "./components/Icons";
import { MenuButton } from "./components/Menu";
import { Sidebar, View } from "./views/Sidebar";
import { TabContent, TabRef } from "./views/Tabs";
import { GitChoiceHost } from "./views/GitCredentials";
import { openInWindow, type AppFeature } from "./api";
import { documentationUrl } from "./documentation";

const HOME: TabRef = { kind: "home", title: "Home" };
type EditorTab = TabRef & { pinned: boolean };
const viewForTab = (tab: TabRef): View => {
  if (tab.kind === "home") return "home";
  if (["workspace", "workspaces", "git-credentials"].includes(tab.kind)) return "workspaces";
  // Workspace runs belong with workspaces; runs of deployed programs with workloads.
  if ((tab.kind === "run" && tab.runScope === "workspace") || (tab.kind === "runs" && tab.ref === "workspace")) return "workspaces";
  if (["run", "runs", "workload", "deploy", "folder", "docs"].includes(tab.kind)) return "workloads";
  if (tab.kind.startsWith("catalog") || ["products", "product", "access-requests", "case"].includes(tab.kind)) return "catalog";
  if (["apis", "api"].includes(tab.kind)) return "apis";
  if (["kafka", "kafka-clusters"].includes(tab.kind)) return "kafka";
  if (["data", "source", "schema", "data-object"].includes(tab.kind)) return "data";
  if (tab.kind === "pool") return "compute";
  if (tab.kind.startsWith("package")) return "packages";
  // Pages that moved to their subject's home; old links still land there.
  if (tab.kind === "admin-sources") return "data";
  if (["repos", "admin-repos"].includes(tab.kind)) return "workloads";
  if (["app-profiles", "admin-app-profiles", "admin-service-accounts"].includes(tab.kind)) return "access";
  if (tab.kind === "admin-classifications") return "privacy";
  if (tab.kind.startsWith("admin-")) return "admin";
  if (tab.kind.startsWith("privacy-")) return "privacy";
  return "access";
};
const key = (t: TabRef) => t.kind === "access" || t.kind === "users" || t.kind === "catalog" || t.kind === "apis" ? `${t.kind}:` : `${t.kind}:${t.ref ?? ""}`;

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [booting, setBooting] = useState(true);
  const [bootError, setBootError] = useState<string | null>(null);

  useEffect(() => {
    currentUser()
      .then(setUser)
      .catch((e: Error) => setBootError(e.message))
      .finally(() => setBooting(false));
  }, []);

  if (booting) return <div className="login muted" role="status">Connecting…</div>;
  if (!user) return <Login error={bootError} />;
  return <ObjectCacheProvider key={user.profile.sub}><Shell user={user} /></ObjectCacheProvider>;
}

function Login({ error }: { error: string | null }) {
  const [server, setServerInput] = useState(getServer());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(error);

  const go = async () => {
    setBusy(true);
    setServer(server);
    resetManager();
    try {
      await signIn();
    } catch (e) {
      setErr((e as Error).message);
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="card">
        <h1>Floren Data Platform</h1>
        <p className="muted">Sign in to your organisation's Floren Data Platform. Code and data stay on the server.</p>
        <form onSubmit={(e) => { e.preventDefault(); void go(); }}>
          {!WEB_MODE && <div className="field">
            <label htmlFor="server">Server</label>
            <input id="server" className="mono" value={server} onChange={(e) => setServerInput(e.target.value)} />
          </div>}
          {err && <p className="error" role="alert">{err}</p>}
          <button className="btn" disabled={busy || !server.trim()}>{busy ? "Redirecting…" : "Sign in"}</button>
        </form>
      </div>
    </div>
  );
}

function Shell({ user }: { user: User }) {
  const [expired, setExpired] = useState(false);
  useEffect(() => onSessionExpired(() => setExpired(true)), []);
  const hub = useHub(!expired);
  const [view, setView] = useState<View>("home");
  const [tabs, setTabs] = useState<EditorTab[]>([{ ...HOME, pinned: false }]);
  const [active, setActive] = useState(key(HOME));
  const [switchingGroup, setSwitchingGroup] = useState(false);
  // After choosing a section the labels close, so the sidebar is free at once; they reopen on the next hover.
  const [picked, setPicked] = useState(false);
  const switchGroup = async (id: string) => {
    setSwitchingGroup(true);
    try { await hub.selectActiveGroup(id); }
    finally { setSwitchingGroup(false); }
  };

  const openSearch = () => {
    // Without All objects in the app profile, search goes to the data products.
    if (!hub.has("catalog.objects")) { if (hub.has("products.browse")) openTab({ kind: "products", title: "Data products" }); return; }
    setView("catalog"); openTab({ kind: "catalog", title: "Catalog" });
    requestAnimationFrame(() => document.getElementById("catalog-search")?.focus());
  };
  const openTab = (t: TabRef) => {
    setView(viewForTab(t));
    setTabs((ts) => {
      if (ts.some((x) => key(x) === key(t))) return ts.map((x) => key(x) === key(t) ? { ...t, pinned: x.pinned } : x);
      const preview = ts.findIndex((x) => !x.pinned);
      return preview < 0 ? [...ts, { ...t, pinned: false }] : ts.map((x, i) => i === preview ? { ...t, pinned: false } : x);
    });
    setActive(key(t));
  };
  const togglePin = (tab: EditorTab) => {
    // Keep at most one replaceable page. Unpinning selects that page and replaces
    // any other preview; the remaining pinned pages stay open.
    setTabs((ts) => ts.filter((x) => !tab.pinned || x.pinned || key(x) === key(tab))
      .map((x) => key(x) === key(tab) ? { ...x, pinned: !x.pinned } : x));
    if (tab.pinned) { setActive(key(tab)); setView(viewForTab(tab)); }
  };
  const closeTab = (k: string) => {
    setTabs((ts) => {
      const next = ts.filter((x) => key(x) !== k);
      if (k === active) { setActive(next.length ? key(next[next.length - 1]) : ""); if (next.length) setView(viewForTab(next[next.length - 1])); }
      return next;
    });
  };
  // A link from a review email, once signed in.
  const signedIn = !!hub.me;
  useEffect(() => {
    if (!signedIn) return;
    let link: string | null = null;
    try { link = sessionStorage.getItem("fdp.link"); sessionStorage.removeItem("fdp.link"); } catch { /* no storage */ }
    if (link?.startsWith("case=")) openTab({ kind: hub.me?.compliance_roles?.length ? "privacy-case" : "case", ref: link.slice(5), title: "Compliance form" });
    else if (link === "memberships") openTab({ kind: "privacy-memberships", title: "Memberships" });
  }, [signedIn]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const search = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(); }
    };
    window.addEventListener("keydown", search);
    return () => window.removeEventListener("keydown", search);
  });

  type Activity = { id: View; label: string; icon: JSX.Element; feature?: AppFeature };
  // Overview (look) → Build (make) → Integration (connect); access and administration sit at the bottom.
  // The active group's app profile decides which of them someone gets.
  const allGroups: { label: string; items: Activity[] }[] = [
    { label: "Overview", items: [
      { id: "home", label: "Home", icon: <IconHome /> },
      { id: "catalog", label: "Catalog", icon: <IconCatalog />, feature: "products.browse" },
      { id: "compute", label: "Usage", icon: <IconCompute />, feature: "usage" },
    ] },
    { label: "Build", items: [
      { id: "workspaces", label: "Workspaces", icon: <IconWorkspaces />, feature: "workspaces" },
      { id: "workloads", label: "Workloads", icon: <IconDeployed />, feature: "workloads.view" },
      { id: "data", label: "Storage", icon: <IconData />, feature: "storage" },
      { id: "packages", label: "Packages", icon: <IconPackage />, feature: "packages" },
    ] },
    { label: "Integration", items: [
      // Kafka is optional per layer: shown once a cluster is visible, or to admins who register one.
      ...(hub.me?.is_admin || hub.resources.some((r) => r.kind === "kafka") ? [{ id: "kafka" as View, label: "Kafka", icon: <IconStream />, feature: "kafka" as AppFeature }] : []),
      { id: "apis", label: "APIs", icon: <IconApi />, feature: "apis" },
    ] },
  ];
  const groups = allGroups.map((g) => ({ ...g, items: g.items.filter((a) => !a.feature || hub.has(a.feature)) }));
  const layer = hub.layers.find((l) => l.id === hub.layerId);
  const activityButton = (a: Activity) => (
    <button key={a.id} aria-label={a.label} aria-current={view === a.id ? "page" : undefined}
            className={view === a.id ? "active" : ""} onClick={() => selectView(a.id)}>
      {a.icon}<span className="activity-label">{a.label}</span>
    </button>
  );
  const current = tabs.find((t) => key(t) === active);
  const selectView = (next: View) => {
    setView(next);
    const defaults: Partial<Record<View, TabRef>> = {
      home: HOME,
      workspaces: { kind: "workspaces", title: "Workspaces" },
      access: { kind: "users", title: "People" },
      workloads: { kind: "folder", ref: "workloads", title: "Workloads" },
      catalog: { kind: "products", title: "Data products" },
      apis: { kind: "apis", ref: "internal", title: "API management" },
      data: { kind: "data", title: "Storage" },
      kafka: { kind: "kafka-clusters", title: "Kafka" },
      compute: { kind: "pool", title: "Usage" },
      admin: { kind: "admin-layers", title: "Layers" },
      privacy: hub.me?.compliance_roles?.some((r) => r !== "membership_approver") ? { kind: "privacy-reviews", title: "Reviews" } : { kind: "privacy-memberships", title: "Memberships" },
    };
    if (defaults[next]) openTab(defaults[next]!);
  };
  const runningWs = hub.workspaces.filter((w) => w.status === "running").length;
  const activeRuns = hub.runs.filter((r) => r.status === "running" || r.status === "pending").length;

  return (
    <div className="shell">
      <nav className={`activity ${picked ? "picked" : ""}`} aria-label="Views" onMouseLeave={() => setPicked(false)} onKeyDown={() => setPicked(false)}
        onClick={(e) => { if ((e.target as HTMLElement).closest(".activity-group > button, .activity > button")) setPicked(true); }}>
        {groups.filter((g) => g.items.length).map((g, i) => <div key={g.label} className="activity-group" role="group" aria-label={g.label}>
          {i > 0 && <hr />}{g.items.map(activityButton)}</div>)}
        <div className="spacer" />
        {(hub.has("security") || hub.has("workspaces")) && activityButton({ id: "access", label: "Access", icon: <IconAdmin /> })}
        {!!hub.me?.compliance_roles?.length && activityButton({ id: "privacy", label: "Privacy", icon: <IconPrivacy /> })}
        {hub.me?.is_admin && activityButton({ id: "admin", label: "Administration", icon: <IconSettings /> })}
        <button aria-label="Documentation" onClick={() => {
          void hub.act(() => openInWindow(documentationUrl(view), "FDP documentation", `fdp-help-${view}`));
        }}><IconBook /><span className="activity-label">Documentation</span></button>
        <MenuButton id="account-menu" label="Account" menuLabel="Account" className="" icon={<><IconAccount /><span className="activity-label">Account</span></>}>
          {(close) => <>
            <div className="menu-header"><strong>{user.profile.preferred_username ?? hub.me?.username}</strong>
              <span className="muted">{new URL(getServer()).host}</span></div>
            <button role="menuitem" onClick={() => { close(); openTab({ kind: "git-credentials", title: "Git credentials" }); }}>Git credentials</button>
            <button role="menuitem" onClick={() => { close(); void signOut(); }}>Sign out</button>
          </>}
        </MenuButton>
      </nav>

      <aside className="sidebar">
        <div className="sidebar-content"><Sidebar key={hub.me?.active_group_id ?? ""} view={view} hub={hub} openTab={openTab} activeKey={active} activeRef={current?.ref} activeIntent={current?.intent} /></div>
        {/* The working context: lists show this group's objects in this layer, and new work is created there. */}
        <div className="context-picker" role="group" aria-label="Working context">
          <label htmlFor="active-group">Group</label>
          <select id="active-group" aria-label="Active group" value={hub.me?.active_group_id ?? ""} disabled={switchingGroup || !hub.me?.available_groups?.length}
            title="Shows this group's objects and owns new objects. Secondary groups still authorize existing-object actions."
            onChange={(e) => void switchGroup(e.target.value)}>
            <option value="" disabled>{hub.me?.available_groups?.length ? "Choose active group" : "No groups available"}</option>
            {hub.me?.available_groups?.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </select>
          {!!hub.layers.length && <>
            <label htmlFor="active-layer">Layer</label>
            <select id="active-layer" aria-label="Layer mode" value={hub.layerId} title="Lists show this layer; what you create lands in it."
              onChange={(e) => void hub.selectActiveLayer(e.target.value)}>
              {!hub.layerId && <option value="" disabled>Choose a layer</option>}
              {hub.layers.map((l) => <option key={l.id} value={l.id}>{l.name}{l.production ? " · production" : ""}</option>)}
            </select>
          </>}
          {switchingGroup && <small role="status">Switching group…</small>}
        </div>
      </aside>

      <main className="main">
        <div className="tabs" role="tablist" aria-label="Open editors">
          {tabs.map((t) => (
            <div key={key(t)} role="tab" aria-label={t.title} aria-selected={key(t) === active} tabIndex={key(t) === active ? 0 : -1} title={t.title}
                 className={`tab ${t.pinned ? "pinned" : "preview"} ${key(t) === active ? "active" : ""}`} onClick={() => { setActive(key(t)); setView(viewForTab(t)); }}
                 onAuxClick={(e) => { if (e.button === 1) { e.preventDefault(); closeTab(key(t)); } }}
                 onKeyDown={(e) => {
                   const i = tabs.indexOf(t);
                   const move = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
                   if (move && tabs.length > 1) {
                     e.preventDefault();
                     const next = tabs[(i + move + tabs.length) % tabs.length];
                     setActive(key(next)); setView(viewForTab(next));
                     (e.currentTarget.parentElement?.children[tabs.indexOf(next)] as HTMLElement | undefined)?.focus();
                   }
                   if (e.key === "Delete") closeTab(key(t));
                 }}>
              <span className="tab-label">{t.title}</span>
              <button className="icon-btn tab-pin" aria-label={`${t.pinned ? "Unpin" : "Pin"} ${t.title}`} aria-pressed={t.pinned}
                title={t.pinned ? "Unpin page · allow it to be replaced" : "Pin page · keep it open"}
                onClick={(e) => { e.stopPropagation(); togglePin(t); }}><IconPin /></button>
              <button className="icon-btn close" aria-label={`Close ${t.title}`} title="Close"
                      onClick={(e) => { e.stopPropagation(); closeTab(key(t)); }}>
                <IconClose />
              </button>
            </div>
          ))}
        </div>
        <div className="content">
          {current
            ? <div className={`page ${current.kind === "docs" ? "wide" : ""}`}><TabContent key={`${key(current)}:${hub.me?.active_group_id ?? ""}`} tab={current} hub={hub} openTab={openTab} /></div>
            : <div className="empty-editor"><div><p>No editor open.</p><p>Choose an item in the sidebar, or press <span className="kbd">⌘K</span> to search the catalog.</p></div></div>}
        </div>
      </main>

      <GitChoiceHost />
      {expired && <div className="modal-backdrop">
        <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="expired-title" aria-describedby="expired-text">
          <h2 id="expired-title">Session expired</h2>
          <p id="expired-text" className="muted">Your sign-in could not be renewed, for example after a long break or when the identity
            provider ended the session. Sign in again to continue; workspaces and runs keep running on the server.</p>
          <div className="form-actions"><button className="btn" autoFocus onClick={() => void signIn()}>Sign in again</button>
            <button className="btn secondary" onClick={() => void signOut()}>Sign out</button></div>
        </div>
      </div>}
      <footer className="statusbar">
        <span>{new URL(getServer()).host}</span>
        <span>{user.profile.preferred_username ?? hub.me?.username}</span>
        {layer && <span>{hub.me?.available_groups?.find((g) => g.id === hub.me?.active_group_id)?.name ?? "No group"} · {layer.name}</span>}
        {hub.error && <span className="status-error" title={hub.error}>⚠ {hub.error}</span>}
        <div className="spacer" />
        <span>{runningWs} workspace{runningWs === 1 ? "" : "s"} running</span>
        <span>{activeRuns} of your runs active</span>
      </footer>
    </div>
  );
}
