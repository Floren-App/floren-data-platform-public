import { useEffect, useState } from "react";
import { AccessGrantRow, api, ComplianceRoleHeld, JustifyDeadline } from "../api";
import { PageHeader } from "../components/Page";
import type { Hub } from "../hub";
import { OpenTab, resourcePath, resourceTab } from "../components/Permissions";
import { ProductIcon } from "../components/ProductIcon";

const day = (v: string | null) => v ? new Date(v).toLocaleDateString() : "";

/** What a group, person or service identity can reach. Rows follow the layer mode; objects without a layer
 * (workloads root, packages) always show. Each row opens that object's Permissions tab. */
export function HasAccessTo({ rows, hub, openTab, showGroup = false, label = "Has access to" }: { rows: AccessGrantRow[]; hub: Hub; openTab: OpenTab;
  showGroup?: boolean; label?: string }) {
  const inMode = rows.filter((r) => !r.resource.layer_id || !hub.layerId || r.resource.layer_id === hub.layerId);
  const hidden = rows.length - inMode.length;
  return <>
    <div className="table-scroll"><table aria-label={label}><thead><tr><th>Object</th><th>Role</th><th>Via</th>{showGroup && <th>Group</th>}<th /></tr></thead><tbody>
      {inMode.map((r, i) => <tr key={`${r.resource.id}/${r.role}/${r.group?.id ?? ""}/${i}`}>
        <td><button className="text-button object-name" onClick={() => openTab(resourceTab(r.resource, "permissions"))}>
          <ProductIcon kind={String(r.resource.config.source_kind ?? r.resource.kind)} size={16} />{resourcePath(r.resource, hub.resources) || r.resource.name}</button>
          {r.resource.layer_name && <span className="muted small"> · {r.resource.layer_name}</span>}</td>
        <td><span className="badge role-badge">{r.role}</span></td>
        <td>{r.via === "product" ? <>data product {r.product ? <button className="text-button" onClick={() => openTab({ kind: "product", ref: r.product!.id, title: r.product!.title })}>{r.product.title}</button> : ""}</> : "direct"}
          {r.until && <div className="muted small">until {day(r.until)} (approved form)</div>}
          {r.deadline && <div className="error-text small">needs a form by {day(r.deadline)}</div>}
          {r.pending_membership && <div className="error-text small">waits for membership approval</div>}</td>
        {showGroup && <td>{r.group ? <button className="text-button" onClick={() => openTab({ kind: "access", ref: r.group!.id, title: r.group!.name })}>{r.group.name}</button> : <span className="muted">directly</span>}</td>}
        <td className="table-actions"><button className="btn sm secondary" onClick={() => openTab(resourceTab(r.resource, "permissions"))}>Permissions</button></td>
      </tr>)}
      {!inMode.length && <tr><td colSpan={showGroup ? 5 : 4} className="muted">{rows.length ? "Nothing in this layer." : "No roles on objects you can see."}</td></tr>}
    </tbody></table></div>
    {hidden > 0 && <p className="muted small">{hidden} more in other layers. Switch the layer at the bottom of the sidebar to see them.</p>}
  </>;
}

/** Review duties held (compliance roles), with their scope. */
export function ComplianceRolesHeld({ roles, openTab, showGroup = false }: { roles: ComplianceRoleHeld[]; openTab: OpenTab; showGroup?: boolean }) {
  if (!roles.length) return null;
  const scope = (r: ComplianceRoleHeld) => [r.scope.layers?.length ? `${r.scope.layers.length} layer${r.scope.layers.length === 1 ? "" : "s"}` : "",
    r.scope.classifications?.length ? r.scope.classifications.join(", ") : "", r.scope.resource_ids?.length ? `${r.scope.resource_ids.length} objects` : "",
    r.scope.group_ids?.length ? `${r.scope.group_ids.length} groups` : ""].filter(Boolean).join(" · ") || "everything";
  return <section><h3>Review duties</h3>
    <ul className="object-links">{roles.map((r, i) => <li key={i}><ProductIcon kind="identity" size={16} />{r.label} <span className="muted">· {scope(r)}</span>
      {showGroup && <> <span className="muted">via</span> <button className="text-button" onClick={() => openTab({ kind: "access", ref: r.group.id, title: r.group.name })}>{r.group.name}</button></>}</li>)}</ul>
    <p className="hint">Given under Administration → Review duties.</p>
  </section>;
}

/** Access → To do → Needs a form: access that existed before its policy (or whose data was classified higher)
 * and is removed at its deadline unless a compliance form is approved. Justify opens where the form is filled in. */
export function DeadlinesView({ hub, openTab }: { hub: Hub; openTab: OpenTab }) {
  const [rows, setRows] = useState<JustifyDeadline[] | null>(null), [error, setError] = useState("");
  useEffect(() => { api.compliance.deadlines().then(setRows).catch((e: Error) => setError(e.message)); }, []);
  const justify = (d: JustifyDeadline) => {
    if (d.product_id) return openTab({ kind: "product", ref: d.product_id, title: d.resource, productSection: "options" });
    const resource = hub.resources.find((r) => r.id === d.resource_id);
    if (resource) openTab(resourceTab(resource, "permissions"));
  };
  const soon = (v: string) => new Date(v).getTime() - Date.now() < 7 * 86400000;
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Needs a form"
      subtitle="Access that needs an approved compliance form and has none: it existed before its policy, or its data was classified higher since. It stays until the deadline, then it is removed. Justify opens where you fill in the form." />
    {error && <p className="error" role="alert">{error}</p>}
    {!rows ? <p className="muted" role="status">Loading…</p> : <div className="table-scroll"><table aria-label="Needs a form"><thead><tr>
      <th>Group</th><th>Access</th><th>Data</th><th>Deadline</th><th /></tr></thead><tbody>
      {rows.map((d) => <tr key={d.resource_id + d.app_group_id}>
        <td><button className="text-button" onClick={() => openTab({ kind: "access", ref: d.app_group_id, title: d.group, groupSection: "access" })}>{d.group}</button></td>
        <td>{d.product_id ? <>Consumer of {d.resource}</> : <>{d.role} on {d.resource}</>}</td>
        <td><span className="product-chip">{d.classification}</span></td>
        <td className={soon(d.due_at) ? "error-text" : ""}>{day(d.due_at)}</td>
        <td className="table-actions"><button className="btn sm" onClick={() => justify(d)}>Justify</button></td></tr>)}
      {!rows.length && <tr><td colSpan={5} className="muted">Nothing needs a form.</td></tr>}</tbody></table></div>}
  </>;
}
