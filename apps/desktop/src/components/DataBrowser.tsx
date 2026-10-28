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

export function dataTab(schema: AccessResource, child: DataChild): TabRef {
  const data: DataLocation = { schemaId: schema.id, item: child,
    ...(child.kind === "table" || child.kind === "view" || child.kind === "materialized_view" ? { table: child.name } : { path: child.path }) };
  return { kind: "data-object", ref: JSON.stringify([schema.id, child.kind, child.path ?? child.name]), title: child.name, data };
}

function useChildren(schema: AccessResource, location: DataLocation, active: boolean, after = "") {
  const key = "data:" + JSON.stringify([schema.id, schema.config, schema.actions.slice().sort(), location.table ?? null, location.path ?? "", after]);
  return useCached(key, () => api.dataChildren(schema.id, { ...(location.table !== undefined ? { table: location.table } : {}),
    ...(location.path ? { path: location.path } : {}), ...(after ? { after } : {}) }), active && schema.config.state === "ready");
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
    <button className="tree-label tree-name" onClick={() => { setOpen(true); openTab(tab); }}><ProductIcon kind={child.kind === "folder" ? "folder" : "table"} size={16} />{child.name}</button>
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
      <div className="toolbar-group"><span className="hint">{state.status === "ready" ? "Metadata cached for this session" : "Loads when opened"}</span>
        <button className="btn secondary" disabled={state.status === "loading"} onClick={state.refresh}><IconRefresh size={14} />Refresh {columns ? "columns" : "objects"}</button></div></div>
    {state.status === "idle" || state.status === "loading" ? <p className="muted" role="status">Loading {columns ? "columns" : "objects"}…</p>
      : state.status === "error" ? <p className="error" role="alert">{state.error}</p>
      : <div className="table-scroll"><table><thead><tr><th>Name</th><th>{columns ? "Data type" : "Type"}</th>{columns && <th>Nullable</th>}</tr></thead><tbody>
        {items.map((child) => <tr key={child.name}>
          <td>{columns ? <span className="mono">{child.name}</span> : <button className="text-button object-name" onClick={() => openTab(dataTab(schema, child))}><ProductIcon kind={child.kind === "folder" ? "folder" : child.kind === "file" ? "data" : "table"} size={16} />{child.name}</button>}</td>
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
  const section = tab.section ?? "overview";
  const file = location.item?.kind === "file";
  const parts = location.table !== undefined ? [{ name: location.table }] : (location.path ?? "").split("/").filter(Boolean).map((name, i, all) => ({ name,
    ...(i < all.length - 1 ? { onClick: () => openTab(dataTab(schema, { kind: "folder", name, path: all.slice(0, i + 1).join("/") })) } : {}) }));
  return <>
    <ObjectBreadcrumbs resource={schema} resources={hub.resources} openTab={openTab} trailing={parts} />
    <PageHeader icon={<ProductIcon kind={location.item?.kind ?? "table"} size={22} />} title={location.item?.name ?? tab.title}
      subtitle={<>{location.item ? kindLabel(location.item) : "Data object"} · {schema.name}</>} />
    {schema.layer_name && <p className="hint">Layer · <strong>{schema.layer_name}</strong></p>}
    <ObjectTabs active={section} lineage preview={location.table !== undefined} onChange={(s) => openTab({ ...tab, section: s })} />
    {section === "permissions" ? <><p className="scope-note">This object uses its schema's permissions. Grants apply to every current and future object in <strong>{schema.name}</strong>.</p>
      <PermissionsPanel resource={schema} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} /></>
      : section === "lineage" ? <ObjectLineage resourceId={schema.id} objectPath={location.table ?? location.path ?? location.item?.name} openTab={openTab} />
      : section === "preview" && location.table !== undefined ? <DataPreview schema={schema} table={location.table} />
      : file ? <section className="card"><h2>File metadata</h2><dl className="kv"><dt>Path</dt><dd className="mono">{location.path}</dd>
        <dt>Size</dt><dd>{location.item?.size ?? "—"} bytes</dd><dt>Modified</dt><dd>{location.item?.modified_at ? new Date(location.item.modified_at).toLocaleString() : "—"}</dd></dl></section>
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
