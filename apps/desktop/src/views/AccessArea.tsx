import { useEffect, useState } from "react";
import { AccessGrantRow, api, ApiError, CaseTarget, ComplianceRoleHeld, JustifyDeadline, Product, Requirement } from "../api";
import { JustifyPanel } from "../components/ComplianceForm";
import { PageHeader } from "../components/Page";
import type { Hub } from "../hub";
import { OpenTab, resourcePath, resourceTab, rolesFor } from "../components/Permissions";
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

const GRANTABLE = ["source", "schema", "kafka", "topic", "folder", "service", "api", "api-group", "api-operation", "package-root", "package", "root"];

/** Give a group access from the group's side: a data product with one of its options, or an object with a role.
 * When an access policy covers it, the compliance form opens right here and the access follows its review. */
export function GiveAccess({ group, hub, onDone }: { group: { id: string; name: string }; hub: Hub; onDone: (message: string) => void }) {
  const [what, setWhat] = useState<"product" | "object">("product");
  const [products, setProducts] = useState<Product[]>([]), [productId, setProductId] = useState(""), [option, setOption] = useState("");
  const [resourceId, setResourceId] = useState(""), [role, setRole] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [justify, setJustify] = useState<{ target: CaseTarget; requirement: Requirement } | null>(null);
  useEffect(() => { api.products.list({}).then((r) => setProducts(r.items.filter((p) => p.can_manage))).catch(() => setProducts([])); }, []);
  const objects = hub.resources.filter((r) => GRANTABLE.includes(r.kind) && r.actions.includes("assign")).sort((a, b) => resourcePath(a, hub.resources).localeCompare(resourcePath(b, hub.resources)));
  const resource = objects.find((r) => r.id === resourceId);
  const product = products.find((p) => p.id === productId);
  const roles = resource ? rolesFor(resource).roles : {};
  const give = async (e: React.FormEvent) => {
    e.preventDefault(); setBusy(true); setError(""); setJustify(null);
    const target: CaseTarget = what === "product" ? { kind: "product", app_group_id: group.id, product_id: productId, option_id: option || undefined }
      : { kind: "role", app_group_id: group.id, resource_id: resourceId, role };
    try {
      if (what === "product") await api.products.setConsumer(productId, group.id, option || undefined);
      else await api.access.assign(resourceId, group.id, role);
      await hub.refresh(); onDone(what === "product" ? `${group.name} now uses ${product?.title}.` : `${group.name} is now ${role} on ${resource?.name}.`);
    } catch (err) {
      if (err instanceof ApiError && err.requirement) setJustify({ target, requirement: err.requirement });
      else setError((err as Error).message);
    } finally { setBusy(false); }
  };
  return <section className="card" aria-label={`Give ${group.name} access`}>
    <h3>Give access</h3>
    <div className="segmented" role="group" aria-label="What to give">
      <button type="button" aria-pressed={what === "product"} className={what === "product" ? "active" : ""} onClick={() => setWhat("product")}>A data product</button>
      <button type="button" aria-pressed={what === "object"} className={what === "object" ? "active" : ""} onClick={() => setWhat("object")}>An object</button>
    </div>
    {justify ? <JustifyPanel target={justify.target} requirement={justify.requirement} onCancel={() => setJustify(null)}
      onDone={(text) => { setJustify(null); onDone(text); }} /> : <form onSubmit={(e) => void give(e)}>
      {what === "product" ? <div className="arg-grid">
        <div className="field"><label htmlFor="give-product">Data product</label><select id="give-product" value={productId} onChange={(e) => { setProductId(e.target.value); setOption(""); }}>
          <option value="">Choose a product you own</option>{products.map((p) => <option key={p.id} value={p.id}>{p.title}{p.layer_name ? ` · ${p.layer_name}` : ""}</option>)}</select></div>
        {product && (product.options ?? []).length > 1 && <div className="field"><label htmlFor="give-option">Access option</label><select id="give-option" value={option} onChange={(e) => setOption(e.target.value)}>
          <option value="">The default option</option>{(product.options ?? []).map((o) => <option key={o.id} value={o.id}>{o.name} · {o.classification}</option>)}</select></div>}
      </div> : <div className="arg-grid">
        <div className="field"><label htmlFor="give-object">Object</label><select id="give-object" value={resourceId} onChange={(e) => {
          setResourceId(e.target.value); const r = objects.find((x) => x.id === e.target.value); setRole(r ? rolesFor(r).initial : ""); }}>
          <option value="">Choose an object you own</option>{objects.map((r) => <option key={r.id} value={r.id}>{resourcePath(r, hub.resources) || r.name}</option>)}</select></div>
        {resource && <div className="field"><label htmlFor="give-role">Role</label><select id="give-role" value={role} onChange={(e) => setRole(e.target.value)}>
          {Object.entries(roles).map(([name, text]) => <option key={name} value={name}>{name} · {text}</option>)}</select></div>}
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      <div className="form-actions"><button className="btn" disabled={busy || (what === "product" ? !productId : !resourceId || !role)}>Give access</button></div>
      <p className="hint">Only what you own is listed. When an access policy covers the data, you fill in its compliance form first and the access follows its review.</p>
    </form>}
  </section>;
}
