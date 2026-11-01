import { ObjectLineage } from "./ObjectLineage";
import { objectKindLabel } from "../objectKinds";
import { useState } from "react";
import { api, AccessResource, DataChild, DataLocation } from "../api";
import { useCached } from "../cache";
import type { Hub } from "../hub";
import { ObjectBreadcrumbs, ObjectTabs, OpenTab, PermissionsPanel, resourceTab } from "./Permissions";
import type { TabRef } from "../views/Tabs";
import { ProductIcon } from "./ProductIcon";
import { IconChevron, IconRefresh } from "./Icons";
import { PageHeader } from "./Page";
import { PublishButton } from "../views/Products";

export function dataTab(schema: AccessResource, child: DataChild): TabRef {
  const data: DataLocation = { schemaId: schema.id, item: child,
    ...(child.kind === "table" || child.kind === "view" || child.kind === "materialized_view" ? { table: child.name } : { path: child.path }) };
  return { kind: "data-object", ref: JSON.stringify([schema.id, child.kind, child.path ?? child.name]), title: child.name, data };
}

/** Live metadata from the engine. For SQL schemas the first page also comes from the platform's last listing,
 *  shown at once (refreshing) until the live one arrives: Trino can take seconds per Iceberg schema. */
function useChildren(schema: AccessResource, location: DataLocation, active: boolean, after = "") {
  const key = "data:" + JSON.stringify([schema.id, schema.config, schema.actions.slice().sort(), location.table ?? null, location.path ?? "", after]);
  const options = { ...(location.table !== undefined ? { table: location.table } : {}), ...(location.path ? { path: location.path } : {}), ...(after ? { after } : {}) };
  const enabled = active && schema.config.state === "ready";
  const live = useCached(key, () => api.dataChildren(schema.id, options), enabled);
  const sql = schema.config.source_kind !== "s3" && location.path === undefined && !after;
  const quick = useCached(key + ":cached", () => api.dataChildren(schema.id, { ...options, cached: true }), enabled && sql);
  if (live.status === "ready" || !quick.data?.children.length) return { ...live, refreshing: false };
  // Live still loading (or failed): the last listing, marked as refreshing.
  return { ...live, status: "ready" as const, data: quick.data, error: live.status === "error" ? live.error : undefined, refreshing: live.status !== "error" };
}

const kindLabel = (child: DataChild) => objectKindLabel(child.kind);

/** Each level mounts only when opened; pages and columns share the same session cache. */
export function DataTreeChildren({ schema, location = { schemaId: schema.id }, openTab, after = "" }: {
  schema: AccessResource; location?: DataLocation; openTab: OpenTab; after?: string;
}) {
  const state = useChildren(schema, location, true, after);
  const [more, setMore] = useState(false);
  if (schema.config.state !== "ready") return <p className="tree-message muted">Set up access to browse objects.</p>;
  return <ul className="data-children" aria-label={location.table ? `Columns of ${location.table}` : `Objects in ${schema.name}`}>
    {state.status === "idle" || state.status === "loading" ? <li className="tree-message muted" role="status">Loading…</li>
      : state.status === "error" ? <li className="tree-message"><span className="error">{state.error}</span> <button className="text-button" onClick={state.refresh}>Retry</button></li>
      : <>
        {state.refreshing && <li className="tree-message muted" role="status">Refreshing…</li>}
        {state.data?.children.map((child) => <DataTreeItem key={`${child.kind}:${child.path ?? child.name}`} schema={schema} child={child} openTab={openTab} />)}
        {!state.data?.children.length && <li className="tree-message muted">No {location.table ? "columns" : "objects"}.</li>}
        {state.data?.next_cursor && (more ? <li><DataTreeChildren key={state.data.next_cursor} schema={schema} location={location} openTab={openTab} after={state.data.next_cursor} /></li>
          : <li><button className="text-button tree-more" onClick={() => setMore(true)}>Load more…</button></li>)}
      </>}
  </ul>;
}

function DataTreeItem({ schema, child, openTab }: { schema: AccessResource; child: DataChild; openTab: OpenTab }) {
  const [open, setOpen] = useState(false);
  const expand = child.kind !== "column" && child.kind !== "file";
  if (!expand) return <li>
    {child.kind === "file" ? <button className="tree-leaf" onClick={() => openTab(dataTab(schema, child))}><ProductIcon kind="file" size={16} /><span className="tree-name">{child.name}</span><span className="tree-type">file</span></button>
      : <div className="tree-column"><span className="tree-name">{child.name}</span><span className="muted mono" title={child.data_type}>{child.data_type}</span></div>}
  </li>;
  const tab = dataTab(schema, child);
  return <li><div className="tree-node">
    <button className="tree-expander" aria-label={`${open ? "Collapse" : "Expand"} ${child.kind} ${child.name}`} aria-expanded={open} onClick={() => setOpen(!open)}><IconChevron /></button>
    <button className="tree-label tree-name" onClick={() => { setOpen(true); openTab(tab); }}><ProductIcon kind={child.kind === "folder" ? "directory" : "table"} size={16} />{child.name}</button>
    <span className="muted tree-type">{child.kind === "folder" ? "" : kindLabel(child)}</span>
  </div>{open && <DataTreeChildren schema={schema} location={tab.data} openTab={openTab} />}</li>;
}

export function SchemaTreeNode({ schema, openTab, selected }: { schema: AccessResource; openTab: OpenTab; selected: boolean }) {
  const [open, setOpen] = useState(false);
  return <li><div className={`tree-node ${selected ? "selected" : ""}`}>
    <button className="tree-expander" aria-label={`${open ? "Collapse" : "Expand"} schema ${schema.name}`} aria-expanded={open} onClick={() => setOpen(!open)}><IconChevron /></button>
    <button className="tree-label tree-name" onClick={() => { setOpen(true); openTab(resourceTab(schema)); }}>{schema.name}</button>
  </div>{open && <DataTreeChildren schema={schema} openTab={openTab} />}</li>;
}

export function DataListing({ schema, location = { schemaId: schema.id }, openTab, storage = false }: {
  schema: AccessResource; location?: DataLocation; openTab: OpenTab; storage?: boolean;
}) {
  const [after, setAfter] = useState("");
  const [previous, setPrevious] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const state = useChildren(schema, location, true, after);
  const columns = location.table !== undefined;
  const sourceStorage = storage || schema.config.source_kind === "s3" || location.path !== undefined;
  if (schema.config.state !== "ready") return <p className="muted">Set up access to browse objects in this schema.</p>;
  const items = state.data?.children.filter((child) => child.name.toLowerCase().includes(search.toLowerCase())) ?? [];
  return <section className="data-listing" aria-label={columns ? `Columns of ${location.table}` : `Objects in ${schema.name}`}>
    <div className="section-heading" style={{ justifyContent: "flex-start", gap: 8 }}><h2>{columns ? "Columns" : sourceStorage ? "Folders & files" : "Tables & views"}</h2>
      {state.data && <span className="badge" title="Objects on this page">{state.data.children.length}{state.data.next_cursor ? "+" : ""}</span>}</div>
    <div className="object-toolbar"><input type="search" aria-label={columns ? "Find column" : "Find data object"} placeholder="Filter this page…" value={search} onChange={(e) => setSearch(e.target.value)} />
      <div className="toolbar-group"><span className="hint" role="status">{state.refreshing ? "Showing the last listing · refreshing…" : state.error ? `Showing the last listing · ${state.error}` : state.status === "ready" ? "Metadata cached for this session" : "Loads when opened"}</span>
        <button className="btn secondary" disabled={state.status === "loading" || state.refreshing} onClick={state.refresh}><IconRefresh size={14} />Refresh {columns ? "columns" : "objects"}</button></div></div>
    {state.status === "idle" || state.status === "loading" ? <p className="muted" role="status">Loading {columns ? "columns" : "objects"}…</p>
      : state.status === "error" ? <p className="error" role="alert">{state.error}</p>
      : <div className="table-scroll"><table><thead><tr><th>Name</th><th>{columns ? "Data type" : "Type"}</th>{columns && <th>Nullable</th>}</tr></thead><tbody>
        {items.map((child) => <tr key={child.name}>
          <td>{columns ? <span className="mono">{child.name}</span> : <button className="text-button object-name" onClick={() => openTab(dataTab(schema, child))}><ProductIcon kind={child.kind === "folder" ? "directory" : child.kind === "file" ? "data" : "table"} size={16} />{child.name}</button>}</td>
          <td className={columns ? "mono muted" : ""}>{columns ? child.data_type : kindLabel(child)}</td>
          {columns && <td>{child.nullable ? "Yes" : "No"}</td>}
        </tr>)}{!items.length && <tr><td colSpan={columns ? 3 : 2} className="muted">{search ? "No matching objects on this page." : "No objects found in this scope."}</td></tr>}
      </tbody></table></div>}
    {(previous.length > 0 || state.data?.next_cursor) && <div className="row pagination">
      <span className="muted">Page {previous.length + 1}</span>
      <button className="btn sm secondary" disabled={!previous.length || state.status === "loading"} onClick={() => { setAfter(previous[previous.length - 1]); setPrevious(previous.slice(0, -1)); }}>Previous</button>
      <button className="btn sm secondary" disabled={!state.data?.next_cursor || state.status === "loading"} onClick={() => { setPrevious([...previous, after]); setAfter(state.data!.next_cursor!); }}>Next</button>
    </div>}
  </section>;
}

export function DataObjectView({ tab, hub, openTab }: { tab: TabRef; hub: Hub; openTab: OpenTab }) {
  const location = tab.data;
  const schema = hub.resources.find((r) => r.id === location?.schemaId && r.kind === "schema");
  if (!schema || !location) return <p className="muted">Data object not available.</p>;
  const file = location.item?.kind === "file";
  const section = tab.section ?? (file ? "preview" : "overview");   // a file opens on what is in it
  const parts = location.table !== undefined ? [{ name: location.table }] : (location.path ?? "").split("/").filter(Boolean).map((name, i, all) => ({ name,
    ...(i < all.length - 1 ? { onClick: () => openTab(dataTab(schema, { kind: "folder", name, path: all.slice(0, i + 1).join("/") })) } : {}) }));
  return <>
    <ObjectBreadcrumbs resource={schema} resources={hub.resources} openTab={openTab} trailing={parts} />
    <PageHeader icon={<ProductIcon kind={location.item?.kind === "folder" ? "directory" : location.item?.kind ?? "table"} size={22} />} title={location.item?.name ?? tab.title}
      subtitle={<>{location.item ? kindLabel(location.item) : "Data object"} · {schema.name}</>}
      actions={location.table !== undefined && <PublishButton resourceId={schema.id} objectPath={location.table} openTab={openTab} hub={hub} />} />
    {schema.layer_name && <p className="hint">Layer · <strong>{schema.layer_name}</strong></p>}
    <ObjectTabs active={section} lineage preview={location.table !== undefined || file} onChange={(s) => openTab({ ...tab, section: s })} />
    {section === "permissions" ? <><p className="scope-note">This object uses its schema's permissions. Grants apply to every current and future object in <strong>{schema.name}</strong>.</p>
      <PermissionsPanel resource={schema} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} /></>
      : section === "lineage" ? <ObjectLineage resourceId={schema.id} objectPath={location.table ?? location.path ?? location.item?.name} openTab={openTab} />
      : section === "preview" && location.table !== undefined ? <DataPreview schema={schema} table={location.table} />
      : section === "preview" && file && location.path ? <FilePreviewPanel schema={schema} path={location.path} />
      : file ? <><section className="card"><h2>File metadata</h2><dl className="kv"><dt>Path</dt><dd className="mono">{location.path}</dd>
        <dt>Size</dt><dd>{formatBytes(location.item?.size)}</dd><dt>Modified</dt><dd>{location.item?.modified_at ? new Date(location.item.modified_at).toLocaleString() : "—"}</dd></dl></section>
        <UseInCode schema={schema} path={location.path ?? ""} resources={hub.resources} /></>
      : <DataListing key={tab.ref} schema={schema} location={location} openTab={openTab} />}
  </>;
}

function DataPreview({ schema, table }: { schema: AccessResource; table: string }) {
  const state = useCached("data:preview:" + JSON.stringify([schema.id, table, schema.actions.slice().sort()]), () => api.dataPreview(schema.id, table));
  const value = (v: unknown) => v === null ? <span className="muted">NULL</span> : typeof v === "object" ? JSON.stringify(v) : String(v);
  return <section aria-label={`Data preview of ${table}`}>
    <div className="section-heading" style={{ marginTop: 0 }}><div><h2>Data preview</h2><p className="muted">Up to 25 rows using your current data permissions. Row order is not guaranteed.</p></div>
      <button className="btn secondary" disabled={state.status === "loading"} onClick={state.refresh}><IconRefresh size={14} />Refresh preview</button></div>
    {state.status === "loading" || state.status === "idle" ? <p className="muted" role="status">Loading preview…</p> : state.error ? <p className="error" role="alert">{state.error}</p> : <>
      <div className="preview-table"><table><thead><tr>{state.data?.columns.map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
        <tbody>{state.data?.rows.map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j} title={typeof v === "string" ? v : undefined}>{value(v)}</td>)}</tr>)}</tbody></table></div>
      <p className="hint">{state.data?.rows.length ?? 0} rows shown.</p>
    </>}
  </section>;
}

const formatBytes = (n?: number | null) => n == null ? "—" : n < 1024 ? `${n} bytes` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`;

/** The start of a file: tables show their first rows, JSON is formatted, text shows its first part, images show. */
function FilePreviewPanel({ schema, path }: { schema: AccessResource; path: string }) {
  const state = useCached("data:file:" + JSON.stringify([schema.id, path]), () => api.dataFile(schema.id, path));
  const p = state.data;
  const value = (v: unknown) => v === null || v === undefined ? <span className="muted">NULL</span> : typeof v === "object" ? JSON.stringify(v) : String(v);
  return <section aria-label={`Preview of ${path}`}>
    <div className="section-heading" style={{ marginTop: 0 }}><div><h2>Preview</h2>
      <p className="muted">The start of the file, read with your data permissions and recorded in the audit log. To work with all of it, read it in code (Overview).</p></div>
      <button className="btn secondary" disabled={state.status === "loading"} onClick={state.refresh}><IconRefresh size={14} />Refresh</button></div>
    {state.status === "loading" || state.status === "idle" ? <p className="muted" role="status">Loading preview…</p>
      : state.error ? <p className="error" role="alert">{state.error}</p>
      : !p ? null
      : p.kind === "table" ? <>
        <div className="preview-table"><table aria-label="File rows"><thead><tr>{(p.columns ?? []).map((c, i) => <th key={i}>{c}{p.types?.[i] && <div className="muted small">{p.types[i]}</div>}</th>)}</tr></thead>
          <tbody>{(p.rows ?? []).map((row, i) => <tr key={i}>{row.map((v, j) => <td key={j}>{value(v)}</td>)}</tr>)}
            {!(p.rows ?? []).length && <tr><td colSpan={(p.columns ?? []).length || 1} className="muted">No rows.</td></tr>}</tbody></table></div>
        <p className="hint">First {(p.rows ?? []).length} rows{p.total_rows != null ? ` of ${p.total_rows.toLocaleString()}` : p.truncated ? " (from the start of the file)" : ""}.</p></>
      : p.kind === "image" ? <img className="file-image" alt={path} src={`data:${p.content_type};base64,${p.data}`} />
      : p.kind === "none" ? <p className="muted">{p.note}</p>
      : <><pre className="code file-text">{p.text}</pre>{p.truncated && <p className="hint">Only the start of the file is shown.</p>}</>}
  </section>;
}

/** How to read this file in a workspace or workload: the connection name and its path there. */
function UseInCode({ schema, path, resources }: { schema: AccessResource; path: string; resources: AccessResource[] }) {
  const source = resources.find((r) => r.id === schema.parent_id);
  const namespace = typeof schema.config.prefix === "string" ? schema.config.prefix : schema.name;
  const full = [namespace, path].filter(Boolean).join("/");
  const ext = path.split(".").pop()?.toLowerCase();
  const read = ext === "csv" ? "read_csv" : ext === "json" ? "read_json" : ext === "parquet" ? "read_parquet" : ext === "jsonl" || ext === "ndjson" ? "read_jsonl" : "read_text";
  const readR = ext === "csv" ? "read_csv" : ext === "json" ? "read_json" : ext === "parquet" ? "read_raw" : "read_text";
  if (!source) return null;
  return <section className="card"><h2>Use in code</h2>
    <p className="muted">In a workspace or workload, with the same permissions.</p>
    <h3>Python</h3><pre className="code">{`import fdp\n\nfiles = fdp.files(${JSON.stringify(source.name)})\ndata = files.${read}(${JSON.stringify(full)})`}</pre>
    <h3>R</h3><pre className="code">{`files <- fdp::files(${JSON.stringify(source.name)})\ndata <- files$${readR}(${JSON.stringify(full)})`}</pre>
  </section>;
}
