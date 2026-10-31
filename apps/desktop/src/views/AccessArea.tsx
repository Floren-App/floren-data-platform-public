import { AccessGrantRow, ComplianceRoleHeld } from "../api";
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

/** Compliance roles held, with their scope. */
export function ComplianceRolesHeld({ roles, openTab, showGroup = false }: { roles: ComplianceRoleHeld[]; openTab: OpenTab; showGroup?: boolean }) {
  if (!roles.length) return null;
  const scope = (r: ComplianceRoleHeld) => [r.scope.layers?.length ? `${r.scope.layers.length} layer${r.scope.layers.length === 1 ? "" : "s"}` : "",
    r.scope.classifications?.length ? r.scope.classifications.join(", ") : "", r.scope.resource_ids?.length ? `${r.scope.resource_ids.length} objects` : "",
    r.scope.group_ids?.length ? `${r.scope.group_ids.length} groups` : ""].filter(Boolean).join(" · ") || "everything";
  return <section><h3>Compliance roles</h3>
    <ul className="object-links">{roles.map((r, i) => <li key={i}><ProductIcon kind="identity" size={16} />{r.label} <span className="muted">· {scope(r)}</span>
      {showGroup && <> <span className="muted">via</span> <button className="text-button" onClick={() => openTab({ kind: "access", ref: r.group.id, title: r.group.name })}>{r.group.name}</button></>}</li>)}</ul>
    <p className="hint">Given in the privacy area (Privacy → Compliance roles).</p>
  </section>;
}
