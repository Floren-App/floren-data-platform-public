import { ObjectLineage } from "../components/ObjectLineage";
import { objectKindLabel } from "../objectKinds";
import { useCallback, useEffect, useState } from "react";
import { ago, openInWindow, api, AccessResource, ApiError, startingPool, usablePools, programRuntime, Workload, WorkloadCommit, WorkloadVersions, PullResult, Repo, Run, runtimeLabel, ServiceAccount, workloadStatus, workloadDot } from "../api";
import { AccessError } from "../components/AccessError";
import { ProductIcon } from "../components/ProductIcon";
import type { Hub } from "../hub";
import { IconBranch, IconOpen, IconPlay, IconPlus, IconRefresh, IconTrash } from "../components/Icons";
import { PageHeader } from "../components/Page";
import { ArgField } from "./Programs";
import type { TabRef } from "./Tabs";
import { ObjectBreadcrumbs, ObjectTabs, PermissionsPanel, resourcePath } from "../components/Permissions";
import { OrganizationSettings } from "./Objects";
import { LogView } from "../components/LogView";
import { PublishButton } from "./Products";
import { LoadMoreRuns } from "./Sidebar";

const short = (sha?: string | null) => (sha ? sha.slice(0, 7) : "—");
const when = (iso?: string | null) => (iso ? new Date(iso).toLocaleString() : "—");
/** "KEY=value" lines <-> object (for production env vars and secret references). */
const parseLines = (text: string) => Object.fromEntries(text.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#"))
  .map((l) => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]).filter(([k]) => k));
const toLines = (o: Record<string, string>) => Object.entries(o).map(([k, v]) => `${k}=${v}`).join("\n");
const KIND_TEXT: Record<string, string> = {
  app: "Runs as a shared app. Data requests use either each logged-in viewer's permissions or the configured service identity.",
  airflow: "Runs Airflow at its own address. DAG tasks run programs on warehouses as the identity it runs as.",
  dbt: "Builds in the production schema. Its docs (with lineage) are deployed at its address and refreshed by every run.",
  script: "Runs on a warehouse using caller permissions or a configured service identity. Viewers see their own runs.",
  notebook: "Runs on a warehouse using caller permissions or a configured service identity. Viewers see their own runs.",
};

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40);

export const workloadTab = (p: Workload): TabRef => ({ kind: "workload", ref: p.id, title: p.title });

/** Deploy tab: repo → branch → pull → program → deploy. */
export function DeployWizard({ hub, openTab, initialFolder }: { hub: Hub; openTab: (t: TabRef) => void; initialFolder?: string }) {
  const [repos, setRepos] = useState<Repo[] | null>(null);
  const [repoId, setRepoId] = useState("");
  const [branches, setBranches] = useState<{ name: string; sha: string }[]>([]);
  const [branch, setBranch] = useState("");
  const [pulled, setPulled] = useState<PullResult | null>(null);
  const [program, setProgram] = useState("");
  const [slug, setSlug] = useState("");
  const [title, setTitle] = useState("");
  const [warehouse, setWarehouse] = useState("");
  const [folders, setFolders] = useState<AccessResource[]>([]);
  const [folderId, setFolderId] = useState(initialFolder ?? "workloads");
  const [layerId, setLayerId] = useState("");
  const inheritedLayer = folders.find((f) => f.id === folderId)?.layer_id;
  const selectedLayer = inheritedLayer || layerId || hub.layerId;
  const [executionMode, setExecutionMode] = useState<"caller" | "fixed">("caller");
  const [autoUpdate, setAutoUpdate] = useState(true);
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [runAs, setRunAs] = useState("");
  const [envText, setEnvText] = useState("");
  const [secretsText, setSecretsText] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | Error | null>(null);

  useEffect(() => {
    api.repos().then((r) => { setRepos(r); if (r[0]) setRepoId(r[0].id); }).catch((e: Error) => setError(e));
    api.serviceAccounts().then(setAccounts).catch(() => undefined);
    api.access.resources().then((r) => {
      const available = r.filter((f) => (f.kind === "folder" || f.kind === "root") && f.actions.includes("create"));
      setFolders(available);
      setFolderId((selected) => available.some((f) => f.id === selected) ? selected : available[0]?.id ?? "");
    }).catch((e: Error) => setError(e));
  }, []);

  useEffect(() => {
    setBranches([]); setBranch(""); setPulled(null);
    if (!repoId) return;
    setBusy("Loading branches…");
    api.branches(repoId).then((b) => { setBranches(b); setBranch(b[0]?.name ?? ""); setError(null); })
      .catch((e: Error) => setError(e)).finally(() => setBusy(null));
  }, [repoId]);

  const pull = async () => {
    setBusy("Pulling…"); setError(null); setPulled(null);
    try {
      const p = await api.pull(repoId, branch);
      setPulled(p);
      const first = Object.keys(p.programs)[0] ?? "";
      choose(first, p);
    } catch (e) { setError(e as Error); }
    setBusy(null);
  };

  const choose = (name: string, p = pulled) => {
    setProgram(name);
    const def = p?.programs[name];
    if (def?.kind === "stream") setExecutionMode("fixed");
    const base = slugify(name);
    const used = new Set(hub.workloads.map((pub) => pub.slug));
    let address = base;
    let suffix = 1;
    while (used.has(address)) {
      const tail = `-${slugify(branch).slice(0, 14) || "version"}${suffix > 1 ? `-${suffix}` : ""}`;
      address = base.slice(0, 40 - tail.length) + tail;
      suffix += 1;
    }
    setSlug(address);
    setTitle(def?.description ? name : name);
    setWarehouse(startingPool(hub.me?.pools, def?.warehouse, selectedLayer, def?.kind));
  };

  const deploy = async () => {
    setBusy("Deploying…"); setError(null);
    try {
      const pub = await api.deploy({
        repo_id: repoId, branch, program, slug, title: title || program, warehouse: warehouse || undefined,
        allowed_groups: [], layer_id: selectedLayer, folder_id: folderId, execution_mode: executionMode, auto_update: autoUpdate,
        run_as: executionMode === "fixed" ? runAs || undefined : undefined, env: parseLines(envText), secrets: parseLines(secretsText),
      });
      await hub.refresh();
      openTab(workloadTab(pub));
    } catch (e) {
      const err = e as ApiError;
      setError(err.fieldErrors && Object.keys(err.fieldErrors).length ? `${err.message}: ${Object.values(err.fieldErrors).join("; ")}` : err);
    }
    setBusy(null);
  };

  const def = pulled?.programs[program];
  const repo = repos?.find((r) => r.id === repoId);

  return (
    <>
      <PageHeader icon={<IconBranch size={22} />} title="Deploy"
        subtitle="Workspaces are private. To share a program or app, push it to git and deploy it from here. Floren Data Platform keeps it in sync with the branch: new commits are deployed automatically." />

      <div className="card">
        <h3>1 · Source</h3>
        {repos && repos.length === 0 && <p className="muted">No git repositories are registered for you yet. Ask an admin to add one.</p>}
        <div className="arg-grid">
          <div className="field">
            <label htmlFor="pub-repo">Repository</label>
            <select id="pub-repo" value={repoId} onChange={(e) => setRepoId(e.target.value)}>
              {repos?.map((r) => <option key={r.id} value={r.id}>{r.name}</option>)}
            </select>
            {repo && <span className="hint mono">{repo.url}</span>}
          </div>
          <div className="field">
            <label htmlFor="pub-branch">Branch</label>
            <select id="pub-branch" value={branch} onChange={(e) => { setBranch(e.target.value); setPulled(null); }}>
              {branches.map((b) => <option key={b.name} value={b.name}>{b.name} · {short(b.sha)}</option>)}
            </select>
          </div>
        </div>
        <div className="form-actions" style={{ marginTop: 4 }}>
          <button className={`btn ${pulled ? "secondary" : ""}`} onClick={pull} disabled={!repoId || !branch || !!busy}>
            <IconBranch size={14} />{busy === "Pulling…" ? "Pulling…" : "Pull"}
          </button>
          {busy && busy !== "Pulling…" && <span className="hint" role="status">{busy}</span>}
        </div>
      </div>

      {pulled && (
        <div className="card">
          <h3>2 · Program</h3>
          <p className="muted mono" style={{ marginTop: 0 }}>
            {[short(pulled.commit.sha), pulled.commit.subject, pulled.commit.author, pulled.commit.date && when(pulled.commit.date)].filter(Boolean).join(" · ")}
          </p>
          {pulled.errors.length > 0 && <ul className="error">{pulled.errors.map((e) => <li key={e}>{e}</li>)}</ul>}
          {Object.keys(pulled.programs).length === 0
            ? <p className="muted">No deployable programs: this commit needs a valid <code>fdp.json</code> at the repository root.</p>
            : (
              <div className="program-tabs" role="tablist">
                {Object.entries(pulled.programs).map(([n, p]) => (
                  <button key={n} role="tab" aria-selected={n === program} className={`chip ${n === program ? "active" : ""}`}
                          onClick={() => choose(n)}>{n} · {p.kind}</button>
                ))}
              </div>
            )}
          {def && (
            <>
              {def.description && <p>{def.description}</p>}
              <p className="muted mono" style={{ marginTop: 0 }}>{def.kind} · {runtimeLabel(programRuntime(def).kind, programRuntime(def).version ?? "default")} · {def.entrypoint}
                {def.layer && ` · required layer: ${hub.layers.find((l) => l.id === def.layer)?.name || def.layer}`}
                {def.data_sources.length > 0 && ` · data: ${def.data_sources.join(", ")}`}</p>
              <p className="hint">{KIND_TEXT[def.kind]}</p>
            </>
          )}
        </div>
      )}

      {def && (
        <div className="card">
          <h3>3 · Deploy</h3>
          <p className="hint">Owner group: <strong>{hub.me?.available_groups?.find((g) => g.id === hub.me?.active_group_id)?.name || "Select an active group in the sidebar"}</strong>. This group must have permission to deploy in the selected folder.</p>
          <div className="arg-grid">
            <div className="field">
              <label htmlFor="pub-title">Title</label>
              <input id="pub-title" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="field">
              <label htmlFor="pub-slug">Address</label>
              <input id="pub-slug" className="mono" value={slug} onChange={(e) => setSlug(slugify(e.target.value))} />
              <span className="hint"><span className="mono">/p/{slug || "…"}/</span> · unique per deployed version, such as finance-sales-tst.</span>
              {hub.workloads.some((p) => p.slug === slug) && <span className="field-error">This address is already deployed. Choose another address.</span>}
            </div>
            <div className="field">
              <label htmlFor="pub-wh">Warehouse</label>
              <select id="pub-wh" value={warehouse} onChange={(e) => setWarehouse(e.target.value)}>
                {!warehouse && <option value="">— choose —</option>}
                {usablePools(hub.me?.pools, selectedLayer, pulled?.programs[program]?.kind).map((p) => <option key={p.id} value={p.name}>{p.name} · {p.cpu} vCPU · {p.memory_gb} GB{p.default ? " (default)" : ""}</option>)}
              </select>
            </div>
            <div className="field">
              <label htmlFor="pub-folder">Workload folder</label>
              <select id="pub-folder" value={folderId} onChange={(e) => setFolderId(e.target.value)}>
                {folders.map((f) => <option key={f.id} value={f.id}>{resourcePath(f, hub.resources)}</option>)}
              </select>
              <span className="hint">Inherits this folder's permissions. Add grants on the workload's Permissions tab after deploying.</span>
            </div>
          </div>
          <p className="hint" style={{ marginTop: 0 }}>Deploy the same program again from another branch to keep tst, acp, and prd versions in this folder or its subfolders.</p>
          <div className="field"><label htmlFor="pub-layer">Layer</label><select id="pub-layer" required value={selectedLayer} disabled={!!inheritedLayer} onChange={(e) => setLayerId(e.target.value)}>{!selectedLayer && <option value="">Choose a layer</option>}{hub.layers.map((layer) => <option key={layer.id} value={layer.id}>{layer.name}</option>)}</select><span className="hint">{inheritedLayer ? "Inherited from this folder." : "The workload and its service identity are permanently bound to this layer."}</span></div>
          <h3>Identity &amp; environment</h3>
          <div className="arg-grid">
            <div className="field">
              <label htmlFor="pub-mode">Data identity</label>
              <select id="pub-mode" disabled={def?.kind === "stream"} value={executionMode} onChange={(e) => setExecutionMode(e.target.value as "caller" | "fixed")}>
                <option value="fixed">Fixed service identity</option><option value="caller">Caller / logged-in viewer</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="pub-runas">Fixed execution identity</label>
              <select id="pub-runas" value={runAs} disabled={executionMode !== "fixed"} onChange={(e) => setRunAs(e.target.value)}>
                <option value="">This workload’s registered service</option>
                {accounts.filter((a) => !a.disabled && (!a.layer_id || a.layer_id === selectedLayer)).map((a) => <option key={a.name} value={a.name}>{a.principal}: {a.description || a.groups.join(", ")}</option>)}
              </select>
              <span className="hint">A service account keeps production running independently of you.</span>
              {executionMode === "fixed" && !runAs && <span className="hint">The new service starts without data permissions. Assign it data roles directly on the required schemas, or use app groups. Apps and dbt may need redeployment after access is configured.</span>}
            </div>
            <div className="field">
              <label htmlFor="pub-env">Production variables (KEY=value per line)</label>
              <textarea id="pub-env" className="short" value={envText} onChange={(e) => setEnvText(e.target.value)} placeholder="LOG_LEVEL=info" />
            </div>
            <div className="field">
              <label htmlFor="pub-secrets">Production secrets (VAR=reference per line)</label>
              <textarea id="pub-secrets" value={secretsText} onChange={(e) => setSecretsText(e.target.value)} className="short" placeholder={runAs ? "SLACK_TOKEN=me/slack   (the service account's)" : "API_KEY=group:finance/api"} />
            </div>
          </div>
          <label className="row">
            <input type="checkbox" checked={autoUpdate} onChange={(e) => setAutoUpdate(e.target.checked)} />
            <span>Keep in sync with <b className="mono">{branch}</b>: deploy new commits automatically</span>
          </label>
          {!folders.length && <p className="error">You need Admin or Owner on a workload folder to deploy here.</p>}
          {error && <AccessError error={error} openTab={openTab} />}
          <div className="form-actions">
            <button className="btn" onClick={deploy} disabled={slug.length < 2 || hub.workloads.some((p) => p.slug === slug) || !!busy || !folderId || !folders.length || (!warehouse && !def.warehouse)}>
              {busy === "Deploying…" ? "Deploying…" : `Deploy ${program}`}
            </button>
          </div>
        </div>
      )}
      {error && !def && <AccessError error={error} openTab={openTab} />}
    </>
  );
}

/** One workload: open/run it, GitOps status and history, settings for its deployer. */
const COMMIT_STATE: Record<WorkloadCommit["state"], [string, string]> = {
  running: ["running", "Running now"], queued: ["queued", "Waiting for capacity"], failed: ["failed", "Deploy failed"],
  deployed: ["stopped", "Ran before"], pending: ["ready", "Not deployed yet"], skipped: ["idle", "Never deployed"],
};

/** Git leads: the branch's commits, newest first, and which of them ran here and when. */
function VersionsPanel({ pub }: { pub: Workload }) {
  const [versions, setVersions] = useState<WorkloadVersions | null>(null), [error, setError] = useState("");
  useEffect(() => {
    let alive = true;
    api.workloadVersions(pub.id).then((v) => { if (alive) { setVersions(v); setError(""); } }).catch((e: Error) => alive && setError(e.message));
    return () => { alive = false; };
  }, [pub.id, pub.deployed_sha, pub.latest_sha, pub.queued_sha, pub.last_checked_at]);
  const history = (pub.events ?? []).filter((e) => ["deployed", "deploy_failed", "queued", "update_found", "sync_failed", "restarting"].includes(e.kind));
  return <>
    <section className="card"><dl className="kv">
      <dt>Source</dt><dd><span className="mono">{pub.repo}</span> · branch <b className="mono">{pub.branch}</b> · {pub.program}</dd>
      <dt>Running</dt><dd>{pub.deployed_sha ? <><span className="mono" title={pub.deployed_sha}>{short(pub.deployed_sha)}</span> · deployed {ago(pub.deployed_at)}{pub.deployed_commit.date && <span className="muted"> · committed {ago(pub.deployed_commit.date)}</span>}</> : "nothing yet"}</dd>
      <dt>Branch head</dt><dd>{pub.latest_sha ? <span className="mono" title={pub.latest_sha}>{short(pub.latest_sha)}</span> : "—"} · checked {ago(pub.last_checked_at)}
        {versions && versions.behind > 0 && <> · <span className="badge warn">{versions.behind} newer commit{versions.behind === 1 ? "" : "s"}</span></>}</dd>
      <dt>Updates</dt><dd>{pub.auto_update ? <>New commits on <b className="mono">{pub.branch}</b> deploy automatically.</> : "Automatic updates are off: Check for updates deploys nothing until they are turned on."}</dd>
    </dl><p className="hint">Git is the source of truth. To change what runs, push to <span className="mono">{pub.branch}</span>; to roll back, revert the commit in Git.</p></section>
    <div className="section-heading"><h2>Commits on {pub.branch}</h2></div>
    {error ? <p className="error" role="alert">{error}</p> : !versions ? <p className="muted" role="status">Reading the branch…</p>
      : <div className="table-scroll"><table aria-label="Versions"><thead><tr><th>Version</th><th>Commit</th><th>Author</th><th>Committed</th><th>Here</th></tr></thead><tbody>
        {versions.commits.map((c) => <tr key={c.sha} className={c.state === "running" ? "current" : ""}>
          <td><span className="mono" title={c.sha}>{short(c.sha)}</span> <button className="icon-btn" aria-label={`Copy commit ${short(c.sha)}`} title="Copy the full commit hash" onClick={() => void navigator.clipboard?.writeText(c.sha)}>⧉</button></td>
          <td>{c.subject}</td><td>{c.author}</td><td title={new Date(c.date).toLocaleString()}>{ago(c.date)}</td>
          <td><span className="status"><span className={`dot ${COMMIT_STATE[c.state][0]}`} />{COMMIT_STATE[c.state][1]}{c.deployed_at && c.state !== "pending" && <span className="muted"> · deployed {ago(c.deployed_at)}</span>}</span></td>
        </tr>)}
      </tbody></table></div>}
    {history.length > 0 && <><div className="section-heading"><h2>Deploy history</h2></div>
      <div className="table-scroll"><table aria-label="Deploy history"><thead><tr><th>When</th><th>What</th><th>Version</th><th>Details</th></tr></thead><tbody>
        {history.map((e, i) => <tr key={i}><td title={new Date(e.at).toLocaleString()}>{ago(e.at)}</td><td>{e.kind.replace("_", " ")}</td><td className="mono">{short(e.sha)}</td><td className="muted">{e.message}</td></tr>)}
      </tbody></table></div></>}
  </>;
}

export function WorkloadView({ id, tab, hub, openTab, runTab }: {
  id: string; tab: TabRef; hub: Hub; openTab: (t: TabRef) => void; runTab: (r: Run) => TabRef;
}) {
  const [pub, setPub] = useState<Workload | null>(null);
  const [error, setError] = useState<string | Error | null>(null);
  const [loadError, setLoadError] = useState<Error | null>(null);
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [pool, setPool] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => api.workload(id).then((p) => { setPub(p); setLoadError(null); })
    .catch((e: Error) => setLoadError(e)), [id]);
  useEffect(() => { load(); const t = setInterval(load, 4000); return () => clearInterval(t); }, [load]);
  useEffect(() => {
    if (!pub) return;
    setValues(Object.fromEntries(pub.program_def.args?.map((a) => [a.name, a.default ?? (a.type === "boolean" ? false : "")]) ?? []) as Record<string, string | boolean>);
    setPool(startingPool(hub.me?.pools, pub.warehouse, pub.layer_id, pub.kind));
  }, [pub?.deployed_sha]); // eslint-disable-line react-hooks/exhaustive-deps

  if (loadError && !pub) return <AccessError error={loadError} openTab={openTab} />;
  if (!pub) return <p className="muted">Loading…</p>;

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try { await fn(); await load(); await hub.refresh(); return true; }
    catch (e) { setError(e as Error); return false; }
    finally { setBusy(false); }
  };
  const run = async () => {
    setBusy(true); setFieldErrors({}); setError(null);
    try {
      const args: Record<string, unknown> = {};
      for (const a of pub.program_def.args ?? []) {
        const v = values[a.name];
        if (v === "" || v === undefined) continue;
        args[a.name] = a.type === "integer" ? parseInt(String(v), 10) : a.type === "number" ? parseFloat(String(v)) : v;
      }
      const r = await api.runDeployed(pub.id, args, pool);
      await hub.refresh();
      openTab(runTab(r));
    } catch (e) {
      setError(e as Error);
      if (e instanceof ApiError) setFieldErrors(e.fieldErrors);
    }
    setBusy(false);
  };
  const behind = pub.latest_sha && pub.deployed_sha && pub.latest_sha !== pub.deployed_sha;
  const myRuns = hub.runs.filter((r) => r.workload_id === pub.id);
  const resource = hub.resources.find((r) => r.id === pub.resource_id);
  const parent = hub.resources.find((r) => r.id === resource?.parent_id);
  const section = tab.section ?? "overview";

  return (
    <>
      {resource && <ObjectBreadcrumbs resource={resource} resources={hub.resources} openTab={openTab} />}
      <PageHeader icon={<ProductIcon kind={pub.kind} runtime={pub.runtime} size={22} />} title={pub.title}
        children={<div className="page-meta">{pub.layer_name && <span className="badge">{pub.layer_name}</span>}{pub.owner_group_name && <span className="badge">Owner: {pub.owner_group_name}</span>}</div>}
        subtitle={<>
          <span className={`dot ${workloadDot(pub)}`} />
          {workloadStatus(pub)} · {objectKindLabel(pub.kind)} · {runtimeLabel(programRuntime(pub.program_def).kind, pub.runtime_version)} · deployed by {pub.owner}
          {pub.deployed_sha && <> · version <button className="text-button mono" title={`${pub.deployed_sha} · deployed ${when(pub.deployed_at)}`} onClick={() => openTab({ ...tab, section: "versions" })}>{short(pub.deployed_sha)}</button> · updated {ago(pub.deployed_at)}</>}
          {pub.run_as && ` · runs as sa:${pub.run_as}`}
        </>}
        actions={<>
          {pub.can_manage && pub.actions.includes("delete") && <>
            <button className="btn ghost danger" disabled={busy} onClick={() => confirm(`Undeploy ${pub.title}? ${pub.kind === "app" ? "The app stops and its address stops working." : "Nobody can run it any more."}`)
              && act(() => api.undeploy(pub.id))}><IconTrash size={14} />Undeploy</button>
            <span className="divider" />
          </>}
          {pub.kind === "app" && <PublishButton resourceId={pub.resource_id} openTab={openTab} />}
          {pub.can_manage && <button className="btn secondary" disabled={busy} title="Check the branch for new commits now"
                  onClick={() => act(() => api.syncWorkload(pub.id))}><IconRefresh size={14} />Check for updates</button>}
          {(pub.kind === "app" || pub.kind === "airflow" || pub.kind === "dbt") && pub.status === "live" && (
            <button className="btn" onClick={() => act(() => openInWindow(api.openWorkload(pub.id).then((r) => r.url), pub.title, `pub-${pub.slug}`))}><IconOpen size={14} />{pub.kind === "airflow" ? "Open Airflow" : pub.kind === "dbt" ? "Open docs" : "Open app"}</button>
          )}
        </>} />

      <ObjectTabs active={section} lineage={!!resource} versions logs settings={pub.actions.includes("identity") || !!parent?.actions.includes("delete")}
        onChange={(s) => openTab({ ...tab, section: s })} />
      {error && <AccessError error={error} openTab={openTab} />}
      {!error && loadError && <AccessError error={loadError} openTab={openTab} />}
      {section === "permissions" && (resource ? <PermissionsPanel resource={resource} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} />
        : <p className="muted">Permission details are unavailable.</p>)}
      {section === "lineage" && resource && <ObjectLineage resourceId={resource.id} openTab={openTab} />}
      {section === "versions" && <VersionsPanel pub={pub} />}
      {section === "logs" && <LogView scope={{ workload_id: pub.id }} openTab={openTab} showWorkload={false} label={`Log of ${pub.title}`} />}
      {section === "settings" && <>
        {pub.actions.includes("identity") && <ProductionSettings pub={pub} onSave={(patch) => act(() => api.updateWorkload(pub.id, patch))} />}
        {resource && parent?.actions.includes("delete") && <OrganizationSettings resource={resource} hub={hub} openTab={openTab} />}
      </>}
      {section === "overview" && <>
      {pub.sync_error && <AccessError error={pub.sync_error} issue={pub.access_issue} openTab={openTab} />}
      {(pub.kind === "script" || pub.kind === "notebook" || pub.kind === "dbt") && <section className="card">
        <h2>Runs on demand</h2><p className="muted">This workload is callable. It uses compute only during a run; deploying it does not keep a script process running.</p>
        <details><summary>Call from an API or Airflow</summary>
          <h3>API</h3><pre className="code">{`POST /api/v1/workloads/${pub.id}/runs\nAuthorization: Bearer <access token>\nContent-Type: application/json\n\n{"args": {}}`}</pre>
          <h3>Airflow</h3><pre className="code">{`from fdp.airflow import FDPRunOperator\n\ntask = FDPRunOperator(\n    task_id="${pub.slug}",\n    workload="${pub.slug}",\n    args={},\n)`}</pre>
          <p className="muted">Supply the program's declared arguments. The caller or Airflow service identity needs Executor on this workload or its parent folder. Data access follows the configured caller/fixed identity mode.</p>
        </details>
      </section>}
      <div className="card">
        <dl className="kv">
          <dt>Source</dt><dd className="mono">{pub.repo} · {pub.branch} · {pub.program}</dd>
          <dt>{pub.kind === "app" || pub.kind === "airflow" ? "Running version" : "Deployed version"}</dt>
          <dd><span className="mono" title={pub.deployed_sha ?? undefined}>{short(pub.deployed_sha)}</span> {pub.deployed_commit.subject && `· ${pub.deployed_commit.subject}`}
            {pub.deployed_commit.author && <span className="muted"> · {pub.deployed_commit.author}</span>}
            {pub.deployed_commit.date && <span className="muted"> · committed {ago(pub.deployed_commit.date)}</span>}
            {" "}<button className="text-button" onClick={() => openTab({ ...tab, section: "versions" })}>All versions</button></dd>
          <dt>Deployed</dt><dd>{when(pub.deployed_at)} <span className="muted">({ago(pub.deployed_at)})</span></dd>
          <dt>GitOps</dt>
          <dd>
            {pub.auto_update ? <>follows <b className="mono">{pub.branch}</b> · last checked {when(pub.last_checked_at)}</> : <>auto-update off · latest <span className="mono">{short(pub.latest_sha)}</span></>}
            {behind && <> <span className="badge warn">update {short(pub.latest_sha)} available</span></>}
          </dd>
          {pub.url && <><dt>Address</dt><dd className="mono">{pub.url}</dd></>}
          <dt>Warehouse</dt><dd>{pub.warehouse}</dd>
          <dt>Data identity</dt><dd>{pub.execution_mode === "caller" ? "Caller / logged-in viewer" : "Fixed service"}</dd>
          <dt>Service identity</dt><dd className="mono">{pub.identity}</dd>
          {Object.keys(pub.env).length > 0 && <><dt>Variables</dt><dd className="mono" style={{ whiteSpace: "pre-wrap" }}>{toLines(pub.env)}</dd></>}
          {Object.keys(pub.secrets).length > 0 && <><dt>Secrets</dt><dd className="mono" style={{ whiteSpace: "pre-wrap" }}>{Object.entries(pub.secrets).map(([k, v]) => `${k} ← ${v}`).join("\n")}</dd></>}
        </dl>
        {pub.can_manage && (
          <label className="row" style={{ marginTop: 16 }}>
            <input type="checkbox" checked={pub.auto_update} disabled={busy}
                   onChange={(e) => act(() => api.updateWorkload(pub.id, { auto_update: e.target.checked }))} />
            Deploy new commits automatically
          </label>
        )}
      </div>

      {(pub.kind === "script" || pub.kind === "notebook" || pub.kind === "dbt") && pub.status === "live" && (
        <div className="card">
          <h2>Run</h2>
          <p className="muted">Runs version {short(pub.deployed_sha)} on a warehouse with <b>{pub.execution_mode === "caller" ? "your" : "the configured service’s"}</b> data access. Only you see your runs.</p>
          <div className="arg-grid">
            {(pub.program_def.args ?? []).map((a) => (
              <ArgField key={a.name} arg={a} value={values[a.name] ?? ""} error={fieldErrors[a.name]}
                        sources={hub.me?.data_sources.map((d) => d.name) ?? []}
                        onChange={(v) => setValues({ ...values, [a.name]: v })} />
            ))}
            <div className="field">
              <label htmlFor="pubrun-pool">Warehouse</label>
              <select id="pubrun-pool" value={pool} onChange={(e) => setPool(e.target.value)}>
                {usablePools(hub.me?.pools, pub.layer_id, pub.kind).map((p) => <option key={p.id} value={p.name}>{p.name} · {p.cpu} vCPU · {p.memory_gb} GB{p.name === pub.warehouse ? " (its warehouse)" : p.default ? " (default)" : ""}</option>)}
              </select>
            </div>
          </div>
          <div className="form-actions" style={{ marginTop: 4 }}>
            <button className="btn" onClick={run} disabled={busy}><IconPlay size={14} />Run {pub.title}</button>
          </div>
          {myRuns.length > 0 && <>
            <h3>Your runs</h3>
            <table>
              <thead><tr><th>Started</th><th>Version</th><th>Warehouse</th><th>Status</th></tr></thead>
              <tbody>{myRuns.map((r) => (
                <tr key={r.id} className="clickable" onClick={() => openTab(runTab(r))}>
                  <td><button className="text-button" onClick={(e) => { e.stopPropagation(); openTab(runTab(r)); }}>{new Date(r.created_at).toLocaleString()}</button></td><td className="mono">{short(r.commit_sha)}</td>
                  <td>{r.pool_name}</td><td><span className="status"><span className={`dot ${r.status}`} />{r.status}</span></td>
                </tr>))}
              </tbody>
            </table>
            {hub.hasMoreRuns && <div className="row pagination"><LoadMoreRuns hub={hub} className="btn sm secondary" /></div>}
          </>}
        </div>
      )}

      <div className="section-heading"><h2>History</h2></div>
      <table>
        <thead><tr><th>When</th><th>Event</th><th>Commit</th><th>Details</th></tr></thead>
        <tbody>{(pub.events ?? []).map((e, i) => (
          <tr key={i}><td className="muted" style={{ whiteSpace: "nowrap" }}>{when(e.at)}</td><td>{e.kind.replace("_", " ")}</td><td className="mono">{short(e.sha)}</td>
            <td style={{ whiteSpace: "pre-wrap" }}>{e.message}</td></tr>))}
          {!pub.events?.length && <tr><td colSpan={4}>No deployment events yet.</td></tr>}
        </tbody>
      </table>
      </>}
    </>
  );
}

/** Deployer: run-as identity and production environment. Saving redeploys long-running kinds. */
function ProductionSettings({ pub, onSave }: { pub: Workload; onSave: (patch: Parameters<typeof api.updateWorkload>[1]) => Promise<boolean> }) {
  const [accounts, setAccounts] = useState<ServiceAccount[]>([]);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState(false);
  const [mode, setMode] = useState<"caller" | "fixed">(pub.execution_mode);
  const [runAs, setRunAs] = useState(pub.run_as ?? "");
  const [env, setEnv] = useState(toLines(pub.env));
  const [secrets, setSecrets] = useState(toLines(pub.secrets));
  useEffect(() => { api.serviceAccounts().then(setAccounts).catch((e: Error) => setError(e.message)); }, []);
  const save = async (e: React.FormEvent) => {
    e.preventDefault(); setSaving(true); setMessage("");
    try {
      if (await onSave({ execution_mode: mode, run_as: mode === "fixed" ? runAs : "", env: parseLines(env), secrets: parseLines(secrets) })) setMessage("Production settings saved.");
    } finally { setSaving(false); }
  };
  return (
    <form className="card" onSubmit={save}>
      <h2>Production settings</h2>
      <p className="muted">Choose whose data access applies when this program runs. Only Owners can change execution identities and secret bindings.</p>
      {error && <p className="error" role="alert">{error}</p>}
      {message && <p className="success-message" role="status">{message}</p>}
      <div className="arg-grid">
        <div className="field"><label htmlFor="ps-mode">Data identity</label>
          <select id="ps-mode" disabled={pub.kind === "stream"} value={mode} onChange={(e) => setMode(e.target.value as "caller" | "fixed")}>
            <option value="caller">Caller / logged-in viewer</option><option value="fixed">Fixed service identity</option>
          </select></div>
        <div className="field"><label htmlFor="ps-runas">Run as</label>
          <select id="ps-runas" value={runAs} disabled={mode !== "fixed"} onChange={(e) => setRunAs(e.target.value)}>
            <option value="">This workload’s registered service</option>
            {accounts.filter((a) => !a.disabled || a.name === runAs).map((a) => <option key={a.name} value={a.name}>{a.principal}{a.disabled ? " (disabled)" : ""}</option>)}
          </select></div>
        <div className="field"><label htmlFor="ps-env">Variables (KEY=value)</label>
          <textarea id="ps-env" className="short" value={env} onChange={(e) => setEnv(e.target.value)} /></div>
        <div className="field"><label htmlFor="ps-sec">Secrets (VAR=reference)</label>
          <textarea id="ps-sec" className="short" value={secrets} onChange={(e) => setSecrets(e.target.value)} /></div>
      </div>
      <div className="form-actions">
        <button className="btn" disabled={saving}>
          Save{pub.kind !== "script" && pub.kind !== "notebook" ? " and redeploy" : ""}</button>
        <button type="button" className="btn secondary" disabled={saving} onClick={() => {
          setMode(pub.execution_mode); setRunAs(pub.run_as ?? ""); setEnv(toLines(pub.env)); setSecrets(toLines(pub.secrets)); setMessage("");
        }}>Reset changes</button>
      </div>
    </form>
  );
}

/** Admin: git repositories people may deploy from (read credentials go to OpenBao). */
export function AdminRepos() {
  const [repos, setRepos] = useState<Repo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const empty = { name: "", url: "", default_branch: "main", description: "", groups: "", username: "", token: "" };
  const [form, setForm] = useState(empty);
  const [editing, setEditing] = useState(false);
  const [open, setOpen] = useState(false);
  const close = () => { setForm(empty); setEditing(false); setOpen(false); };
  const load = () => api.admin.repos().then(setRepos).catch((e: Error) => setError(e.message));
  useEffect(() => { load(); }, []);

  const save = async () => {
    try {
      await api.admin.upsertRepo({
        name: form.name, url: form.url, default_branch: form.default_branch, description: form.description,
        allowed_groups: form.groups.split(",").map((g) => g.trim()).filter(Boolean),
        ...(form.token ? { secret: { username: form.username, token: form.token } } : {}),
      });
      close(); setError(null); load();
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <>
      <PageHeader icon={<IconBranch size={22} />} title="Git repositories"
        subtitle={<>Repositories people may deploy from. Floren Data Platform only ever reads from them: use a <b>read-only</b> token of a service account (GitHub: a fine-grained token with Contents read-only), so it cannot rewrite history. It is stored in the secret store and never shown again. People push from their workspaces with their own tokens (Account → Git credentials).</>}
        actions={<button className="btn" aria-expanded={open && !editing} onClick={() => open && !editing ? close() : (setForm(empty), setEditing(false), setOpen(true))}><IconPlus size={14} />Add repository</button>} />
      {error && <p className="error" role="alert">{error}</p>}
      {open && <form className="card" onSubmit={(e) => { e.preventDefault(); void save(); }}>
        <h3>{editing ? `Edit ${form.name}` : "Add repository"}</h3>
        <div className="arg-grid">
          <div className="field"><label htmlFor="r-name">Name</label><input id="r-name" className="mono" autoFocus={!editing} value={form.name} disabled={editing} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="analytics" /></div>
          <div className="field"><label htmlFor="r-url">Clone URL (https)</label><input id="r-url" className="mono" value={form.url} onChange={(e) => setForm({ ...form, url: e.target.value })} placeholder="https://git.example.com/team/analytics.git" /></div>
          <div className="field"><label htmlFor="r-branch">Default branch</label><input id="r-branch" className="mono" value={form.default_branch} onChange={(e) => setForm({ ...form, default_branch: e.target.value })} /></div>
          <div className="field"><label htmlFor="r-groups">Who may deploy (groups, empty = everyone)</label><input id="r-groups" value={form.groups} onChange={(e) => setForm({ ...form, groups: e.target.value })} /></div>
          <div className="field"><label htmlFor="r-user">Read-only user</label><input id="r-user" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} placeholder="fdp-bot" /></div>
          <div className="field"><label htmlFor="r-token">Token{editing ? " (empty = keep)" : ""}</label><input id="r-token" type="password" autoComplete="off" value={form.token} onChange={(e) => setForm({ ...form, token: e.target.value })} /></div>
        </div>
        <div className="field"><label htmlFor="r-desc">Description</label><input id="r-desc" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></div>
        <div className="form-actions">
          <button className="btn" disabled={!form.name || !form.url}>Save</button>
          <button type="button" className="btn secondary" onClick={close}>Cancel</button>
        </div>
      </form>}
      <table>
        <thead><tr><th>Name</th><th>URL</th><th>Default branch</th><th>Who may deploy</th><th>Credentials</th><th /></tr></thead>
        <tbody>{repos.map((r) => (
          <tr key={r.id}>
            <td><b>{r.name}</b><div className="muted">{r.description}</div></td><td className="mono">{r.url}</td>
            <td className="mono">{r.default_branch}</td><td>{r.allowed_groups.join(", ") || "everyone"}</td>
            <td>{r.has_credentials ? "stored" : <span className="muted">none (public)</span>}</td>
            <td className="table-actions"><div className="row">
              <button className="btn sm secondary" onClick={() => { setEditing(true); setOpen(true); setForm({ ...empty, name: r.name, url: r.url, default_branch: r.default_branch, description: r.description, groups: r.allowed_groups.join(", ") }); requestAnimationFrame(() => document.getElementById("r-url")?.focus()); }}>Edit</button>
              <button className="icon-btn" aria-label={`Remove ${r.name}`} title="Remove repository" onClick={() => confirm(`Remove ${r.name}?`) && api.admin.deleteRepo(r.name).then(load).catch((e: Error) => setError(e.message))}><IconTrash /></button>
            </div></td>
          </tr>))}
          {!repos.length && <tr><td colSpan={6}>No repositories registered yet.</td></tr>}
        </tbody>
      </table>
    </>
  );
}
