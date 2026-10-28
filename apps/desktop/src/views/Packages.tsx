import { useCallback, useEffect, useState } from "react";
import { AccessResource, AllowedPackage, api, PACKAGE_KIND_LABEL, PackageAllowlist, PACKAGE_ORIGIN_LABEL, PackageDetail, PackageImport, PackageKind, PackageList } from "../api";
import type { Hub } from "../hub";
import type { OpenTab } from "../components/Permissions";
import { ObjectTabs, PermissionsPanel } from "../components/Permissions";
import { PageHeader } from "../components/Page";
import { IconPackage, IconPlus, IconRefresh, IconTrash } from "../components/Icons";

const KINDS: PackageKind[] = ["python", "r"];
const size = (n: number) => n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`;
export const packageTab = (kind: PackageKind, name: string) => ({ kind: "package" as const, ref: `${kind}/${name}`, title: name });

/** The package repository: everything anyone can install, and (with the role) publishing and imports. */
export function PackagesView({ hub, openTab, kind }: { hub: Hub; openTab: OpenTab; kind?: PackageKind }) {
  const [data, setData] = useState<PackageList | null>(null), [error, setError] = useState(""), [query, setQuery] = useState("");
  const [panel, setPanel] = useState<"" | "upload" | "import" | "permissions" | "allowlist">("");
  const reload = useCallback(() => api.packages.list(kind).then((d) => { setData(d); setError(""); }).catch((e: Error) => setError(e.message)), [kind]);
  useEffect(() => { void reload(); }, [reload]);
  const can = (a: string) => !!data?.actions.includes(a);
  const root = hub.resources.find((r) => r.id === data?.resource_id);
  const rows = (data?.packages ?? []).filter((p) => !query || p.name.toLowerCase().includes(query.toLowerCase()) || p.summary.toLowerCase().includes(query.toLowerCase()));
  return <>
    <PageHeader icon={<IconPackage size={22} />} title={kind ? `${PACKAGE_KIND_LABEL[kind]} packages` : "Packages"}
      subtitle="The platform's package repository: every version of every package published here or imported from PyPI and CRAN. Workspaces and programs install only from here, never from the internet; everyone can install everything."
      actions={<div className="row">
        {can("import") && <button className="btn" onClick={() => setPanel(panel === "import" ? "" : "import")}><IconPlus size={14} />Import from PyPI or CRAN</button>}
        {can("create") && <button className="btn secondary" onClick={() => setPanel(panel === "upload" ? "" : "upload")}>Publish a package</button>}
        <button className="btn secondary" onClick={() => setPanel(panel === "permissions" ? "" : "permissions")}>Who publishes</button>
        <button className="btn secondary" onClick={() => setPanel(panel === "allowlist" ? "" : "allowlist")}>Allowed packages</button>
      </div>} />
    {error && <p className="error" role="alert">{error}</p>}
    {panel === "import" && data && <ImportForm upstream={data.upstream} kinds={data.kinds ?? KINDS} initialKind={kind} openTab={openTab} onDone={() => setPanel("")} />}
    {panel === "upload" && data && <UploadForm maxMb={data.max_upload_mb} kinds={data.kinds ?? KINDS} initialKind={kind} openTab={openTab} onDone={() => { setPanel(""); void reload(); }} />}
    {panel === "allowlist" && data && <AllowlistPanel kinds={data.kinds ?? KINDS} canEdit={!!hub.me?.is_admin} />}
    {panel === "permissions" && (root ? <PermissionsPanel resource={root} resources={hub.resources} openTab={openTab} onChanged={async () => { await hub.refresh(); await reload(); }} /> : <p className="muted">Loading…</p>)}
    <div className="row" style={{ margin: "12px 0" }}>
      <input type="search" aria-label="Search packages" placeholder="Search packages…" value={query} onChange={(e) => setQuery(e.target.value)} />
      <button className="icon-btn" aria-label="Refresh packages" title="Refresh" onClick={() => void reload()}><IconRefresh /></button>
    </div>
    {data && !rows.length ? <p className="muted">{data.packages.length ? "No package matches." : `No packages yet.${can("import") ? " Import one from PyPI or CRAN, or publish your own." : " Ask a Publisher to import what you need."}`}</p>
    : <table aria-label="Packages"><thead><tr><th>Package</th>{!kind && <th>Language</th>}<th>Latest</th><th>Versions</th><th>Source</th></tr></thead><tbody>
      {rows.map((p) => <tr key={p.id}>
        <td><button className="text-button object-name" onClick={() => openTab(packageTab(p.kind, p.name))}>{p.name}</button><div className="muted">{p.summary}</div></td>
        {!kind && <td>{PACKAGE_KIND_LABEL[p.kind]}</td>}
        <td className="mono">{p.latest ?? <span className="muted">all yanked</span>}</td><td>{p.versions}</td>
        <td><span className="badge">{PACKAGE_ORIGIN_LABEL[p.origin]}</span>{p.owner_group && <span className="muted"> · {p.owner_group}</span>}</td>
      </tr>)}
    </tbody></table>}
  </>;
}

function ImportForm({ upstream, kinds, initialKind, openTab, onDone }: { upstream: PackageList["upstream"]; kinds: PackageKind[]; initialKind?: PackageKind; openTab: OpenTab; onDone: () => void }) {
  const [kind, setKind] = useState<PackageKind>(initialKind ?? "python"), [requirement, setRequirement] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError("");
    try { await api.packages.importPackage(kind, requirement.trim()); onDone(); openTab({ kind: "package-imports", title: "Package imports" }); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return <form className="card" aria-label="Import a package" onSubmit={(e) => void submit(e)}>
    <h3>Import from {kind === "python" ? "PyPI" : "CRAN"}</h3>
    <div className="arg-grid">
      <div className="field"><label htmlFor="pi-kind">Language</label><select id="pi-kind" value={kind} onChange={(e) => setKind(e.target.value as PackageKind)}>
        {kinds.map((k) => <option key={k} value={k} disabled={!upstream[k]}>{PACKAGE_KIND_LABEL[k]}{upstream[k] ? "" : " (turned off)"}</option>)}</select></div>
      <div className="field"><label htmlFor="pi-req">Package</label><input id="pi-req" required placeholder={kind === "python" ? "pandas==2.2.3" : "dplyr or dplyr==1.1.4"}
        value={requirement} onChange={(e) => setRequirement(e.target.value)} /></div>
    </div>
    <p className="muted">{kind === "python"
      ? "The package and its missing dependencies come in as built packages (wheels) for the platform's Python versions and CPU types; no package code runs during the import. Without a version you get the newest."
      : "The source package and its missing dependencies (Depends, Imports, LinkingTo) come from CRAN; older versions from CRAN's archive. Packages that come with R are skipped."}</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !requirement.trim() || !upstream[kind]}>Import</button>
      <button type="button" className="btn secondary" onClick={onDone}>Cancel</button></div>
  </form>;
}

function UploadForm({ maxMb, kinds, initialKind, openTab, onDone }: { maxMb: number; kinds: PackageKind[]; initialKind?: PackageKind; openTab: OpenTab; onDone: () => void }) {
  const [kind, setKind] = useState<PackageKind>(initialKind ?? "python"), [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); if (!file) return; setBusy(true); setError("");
    try { const r = await api.packages.upload(kind, file); onDone(); openTab(packageTab(r.package.kind, r.package.name)); }
    catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  return <form className="card" aria-label="Publish a package" onSubmit={(e) => void submit(e)}>
    <h3>Publish your own package</h3>
    <div className="arg-grid">
      <div className="field"><label htmlFor="pu-kind">Language</label><select id="pu-kind" value={kind} onChange={(e) => setKind(e.target.value as PackageKind)}>
        {kinds.map((k) => <option key={k} value={k}>{PACKAGE_KIND_LABEL[k]}</option>)}</select></div>
      <div className="field"><label htmlFor="pu-file">File</label><input id="pu-file" type="file" required accept={kind === "python" ? ".whl,.tar.gz,.zip" : ".tar.gz"}
        onChange={(e) => setFile(e.target.files?.[0] ?? null)} /></div>
    </div>
    <p className="muted">{kind === "python" ? "A wheel (.whl) or source distribution (.tar.gz) from python -m build. From a workspace: twine upload dist/* (or uv publish), already set up."
      : "A source tarball from R CMD build (name_version.tar.gz)."} Published files never change: publish a new version instead. Up to {maxMb} MB.
      Your active group becomes the package's owner; names that exist on PyPI or CRAN are refused.</p>
    {error && <p className="error" role="alert">{error}</p>}
    <div className="form-actions"><button className="btn" disabled={busy || !file}>Publish</button>
      <button type="button" className="btn secondary" onClick={onDone}>Cancel</button></div>
  </form>;
}

export function PackageView({ hub, openTab, reference }: { hub: Hub; openTab: OpenTab; reference: string }) {
  const [kind, ...rest] = reference.split("/") as [PackageKind, ...string[]];
  const name = rest.join("/");
  const [item, setItem] = useState<PackageDetail | null>(null), [error, setError] = useState("");
  const [section, setSection] = useState<"overview" | "permissions">("overview"), [resource, setResource] = useState<AccessResource | null>(null);
  const reload = useCallback(() => api.packages.get(kind, name).then((d) => { setItem(d); setError(""); }).catch((e: Error) => setError(e.message)), [kind, name]);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { if (section === "permissions" && item?.resource_id) api.access.resource(item.resource_id).then(setResource).catch((e: Error) => setError(e.message)); }, [section, item?.resource_id]);
  const act = async (fn: () => Promise<unknown>) => { setError(""); try { await fn(); await reload(); } catch (e) { setError((e as Error).message); } };
  if (!item) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">Loading…</p>;
  const can = (a: string) => item.actions.includes(a);
  const root = hub.resources.find((r) => r.kind === "package-root");
  return <>
    <PageHeader icon={<IconPackage size={22} />} title={item.name}
      subtitle={<>{PACKAGE_KIND_LABEL[item.kind]} · <span className="badge">{PACKAGE_ORIGIN_LABEL[item.origin]}</span>{item.owner_group && <> · owned by {item.owner_group}</>}{item.summary && <> · {item.summary}</>}</>} />
    {item.origin === "internal" && <ObjectTabs active={section} onChange={(s) => setSection(s as "overview" | "permissions")} overview="Versions" />}
    {error && <p className="error" role="alert">{error}</p>}
    {section === "permissions" ? (resource ? <PermissionsPanel resource={resource} resources={[...hub.resources.filter((r) => r.id !== resource.id), resource]} openTab={openTab} onChanged={reload} /> : <p className="muted">Loading permissions…</p>) : <>
      <p>Install: <code className="mono">{item.install}</code>{item.kind === "r" && <span className="muted"> · a specific version: <code>{`renv::install("${item.name}@<version>")`}</code></span>}</p>
      {item.origin !== "internal" && root && <p className="muted">Imported packages follow the repository's roles: Publishers import new versions.</p>}
      <table aria-label="Versions"><thead><tr><th>Version</th><th>Files</th><th>Requires</th><th /></tr></thead><tbody>
        {item.versions.map((v) => <tr key={v.version} className={v.yanked ? "muted" : ""}>
          <td className="mono">{v.version}{v.yanked && <> <span className="badge">Yanked</span><div className="muted">{v.files[0]?.yanked_reason}</div></>}</td>
          <td>{v.files.map((f) => <div key={f.filename} className="mono" title={`sha256 ${f.sha256} · by ${f.uploaded_by}`}>{f.filename} <span className="muted">{size(f.size)}</span></div>)}</td>
          <td>{v.requires.slice(0, 6).join(", ")}{v.requires.length > 6 && ` +${v.requires.length - 6}`}</td>
          <td className="table-actions"><div className="row">
            {can("yank") && (v.yanked
              ? <button className="btn sm secondary" onClick={() => void act(() => api.packages.unyank(item.kind, item.name, v.version))}>Restore</button>
              : <button className="btn sm secondary" onClick={() => { const reason = prompt(`Yank ${item.name} ${v.version}? Installs stop picking it unless it is pinned exactly. Reason:`); if (reason !== null) void act(() => api.packages.yank(item.kind, item.name, v.version, reason)); }}>Yank</button>)}
            {can("delete") && <button className="icon-btn" aria-label={`Delete ${item.name} ${v.version}`} onClick={() => { if (confirm(`Delete ${item.name} ${v.version} for good? Programs pinned to it stop installing. Yanking is usually better.`)) void act(() => api.packages.deleteVersion(item.kind, item.name, v.version).then(() => item.versions.length === 1 && openTab({ kind: "packages", title: "Packages" }))); }}><IconTrash size={14} /></button>}
          </div></td>
        </tr>)}
      </tbody></table>
    </>}
  </>;
}

export function PackageImportsView() {
  const [jobs, setJobs] = useState<PackageImport[]>([]), [error, setError] = useState("");
  const reload = useCallback(() => api.packages.imports().then((j) => { setJobs(j); setError(""); }).catch((e: Error) => setError(e.message)), []);
  useEffect(() => { void reload(); }, [reload]);
  const busy = jobs.some((j) => j.status === "queued" || j.status === "running");
  useEffect(() => { if (!busy) return; const t = setInterval(() => void reload(), 3000); return () => clearInterval(t); }, [busy, reload]);
  return <>
    <PageHeader icon={<IconPackage size={22} />} title="Package imports" subtitle="Imports from PyPI and CRAN, newest first. They run one at a time; this page follows them."
      actions={<button className="btn secondary" onClick={() => void reload()}><IconRefresh size={14} />Refresh</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {!jobs.length ? <p className="muted">No imports yet.</p>
    : <table aria-label="Package imports"><thead><tr><th>Package</th><th>Status</th><th>Requested</th><th>Added</th></tr></thead><tbody>
      {jobs.map((j) => <tr key={j.id}>
        <td className="mono">{j.requirement} <span className="muted">{PACKAGE_KIND_LABEL[j.kind]}</span></td>
        <td><span className={`badge status-${j.status}`}>{j.status}</span>{j.log && <details><summary>Details</summary><pre className="code">{j.log}</pre></details>}</td>
        <td>{j.requested_by}<div className="muted">{new Date(j.created_at).toLocaleString()}</div></td>
        <td>{j.added.length ? j.added.length === 1 ? j.added[0] : `${j.added.length} files` : "—"}</td>
      </tr>)}
    </tbody></table>}
  </>;
}

/** Which public packages may be imported; administrators edit it (also OpenTofu: fdp_package_allowlist). */
function AllowlistPanel({ kinds, canEdit }: { kinds: PackageKind[]; canEdit: boolean }) {
  const [list, setList] = useState<PackageAllowlist | null>(null), [error, setError] = useState(""), [saved, setSaved] = useState("");
  useEffect(() => { api.packages.allowlist().then(setList).catch((e: Error) => setError(e.message)); }, []);
  if (!list) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">Loading…</p>;
  const edit = (kind: PackageKind, rows: AllowedPackage[]) => { setList({ ...list, [kind]: rows }); setSaved(""); };
  const save = async () => {
    setError(""); setSaved("");
    try { setList(await api.packages.saveAllowlist({ ...list, python: list.python.filter((p) => p.name.trim()), r: list.r.filter((p) => p.name.trim()) })); setSaved("Saved."); }
    catch (e) { setError((e as Error).message); }
  };
  return <section className="card" aria-label="Allowed packages">
    <h3>Allowed packages</h3>
    <p className="muted">While on, public packages can only be imported when they are listed here (their dependencies come along).
      End a name with * for a family, e.g. scikit-*. Packages published here are not affected. Also configurable with OpenTofu (fdp_package_allowlist).</p>
    <label className="row"><input type="checkbox" disabled={!canEdit} checked={list.enabled} onChange={(e) => { setList({ ...list, enabled: e.target.checked }); setSaved(""); }} />
      Only allow listed packages</label>
    {kinds.map((kind) => <div key={kind}>
      <h4>{PACKAGE_KIND_LABEL[kind]}</h4>
      {!list[kind].length && <p className="muted">None listed.</p>}
      {list[kind].map((p, i) => <div key={i} className="row">
        <input aria-label={`${PACKAGE_KIND_LABEL[kind]} package ${i + 1}`} className="mono" disabled={!canEdit} value={p.name} placeholder="pandas"
          onChange={(e) => edit(kind, list[kind].map((x, n) => n === i ? { ...x, name: e.target.value } : x))} />
        <input aria-label={`${PACKAGE_KIND_LABEL[kind]} description ${i + 1}`} disabled={!canEdit} value={p.description} placeholder="Why it is allowed"
          onChange={(e) => edit(kind, list[kind].map((x, n) => n === i ? { ...x, description: e.target.value } : x))} />
        {canEdit && <button className="icon-btn" aria-label={`Remove ${p.name || "package"}`} onClick={() => edit(kind, list[kind].filter((_, n) => n !== i))}><IconTrash size={14} /></button>}
      </div>)}
      {canEdit && <button className="btn sm secondary" onClick={() => edit(kind, [...list[kind], { name: "", description: "" }])}><IconPlus size={14} />Add {PACKAGE_KIND_LABEL[kind]} package</button>}
    </div>)}
    {error && <p className="error" role="alert">{error}</p>}
    {canEdit && <div className="form-actions"><button className="btn" onClick={() => void save()}>Save allowed packages</button>{saved && <span className="muted" role="status">{saved}</span>}</div>}
  </section>;
}
