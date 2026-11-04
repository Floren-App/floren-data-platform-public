import { useEffect, useMemo, useState } from "react";
import { AccessCategory, AccessGrantRow, AccessResource, api, ApiError, CaseTarget, ComplianceRoleHeld, GroupRoleAssignment, JustifyDeadline, Product, Requirement } from "../api";
import { can } from "../permissions";
import { IconPlus } from "../components/Icons";
import { CATEGORIES, CATEGORY_INFO, catalogRoles, catalogTypeKey, Disclosure, Modal, RolePicker, useRoleCatalog } from "../components/Roles";
import { DutyForm } from "./Privacy";
import { PermissionList } from "./RolesView";
import { JustifyPanel } from "../components/ComplianceForm";
import { PageHeader } from "../components/Page";
import type { Hub } from "../hub";
import { OpenTab, PermissionsPanel, resourcePath, resourceTab, rolesFor, roleLabel } from "../components/Permissions";
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
        <td><span className="badge role-badge">{roleLabel(r.role)}</span></td>
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
    if (d.product_id) return openTab({ kind: "product", ref: d.product_id, title: d.resource, productManagement: true, productSection: "options" });
    const resource = hub.resources.find((r) => r.id === d.resource_id);
    if (resource) openTab(resourceTab(resource, "permissions"));
  };
  const soon = (v: string) => new Date(v).getTime() - Date.now() < 7 * 86400000;
  return <>
    <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Needs a form"
      subtitle="Access that needs an approved compliance form and has none: it existed before its policy, or its data was classified higher since. It stays until the deadline, then it is removed. Justify opens where you fill in the form." />
    {error && <p className="error" role="alert">{error}</p>}
    {!rows ? <p className="muted loading" role="status">Loading…</p> : <div className="table-scroll"><table aria-label="Needs a form"><thead><tr>
      <th>Group</th><th>Access</th><th>Data</th><th>Deadline</th><th /></tr></thead><tbody>
      {rows.map((d) => <tr key={d.resource_id + d.app_group_id}>
        <td><button className="text-button" onClick={() => openTab({ kind: "access", ref: d.app_group_id, title: d.group, groupSection: "access" })}>{d.group}</button></td>
        <td>{d.product_id ? <>Data Reader of {d.resource}</> : <>{roleLabel(d.role)} on {d.resource}</>}</td>
        <td><span className="product-chip">{d.classification}</span></td>
        <td className={soon(d.due_at) ? "error-text" : ""}>{day(d.due_at)}</td>
        <td className="table-actions"><button className="btn sm" onClick={() => justify(d)}>Justify</button></td></tr>)}
      {!rows.length && <tr><td colSpan={5} className="muted">Nothing needs a form.</td></tr>}</tbody></table></div>}
  </>;
}

const GRANTABLE = ["source", "schema", "kafka", "topic", "folder", "service", "api", "api-group", "api-operation", "package-root", "package", "root"];

/** Give a group access from the group's side: a data product with one of its options, an object with a role, or a
 * review duty. When an access policy covers it, the compliance form opens right here and the access follows its review. */
export function GiveAccess({ group, hub, onDone, onCancel }: { group: { id: string; name: string }; hub: Hub; onDone: (message: string) => void; onCancel: () => void }) {
  const duties = can(hub.me, "compliance.duties.appoint", "set-duties");
  const [what, setWhat] = useState<"product" | "object" | "duty">("object");
  const [products, setProducts] = useState<Product[]>([]), [productId, setProductId] = useState(""), [option, setOption] = useState("");
  const [resourceId, setResourceId] = useState(""), [role, setRole] = useState(""), [find, setFind] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [justify, setJustify] = useState<{ target: CaseTarget; requirement: Requirement } | null>(null);
  const catalog = useRoleCatalog();
  useEffect(() => { api.products.list({}).then((r) => setProducts(r.items.filter((p) => p.can_manage))).catch(() => setProducts([])); }, []);
  const objects = hub.resources.filter((r) => GRANTABLE.includes(r.kind) && r.actions.includes("assign")).sort((a, b) => resourcePath(a, hub.resources).localeCompare(resourcePath(b, hub.resources)));
  const shown = objects.filter((r) => r.id === resourceId || (resourcePath(r, hub.resources) || r.name).toLowerCase().includes(find.toLowerCase()));
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
      await hub.refresh(); onDone(what === "product" ? `${group.name} now uses ${product?.title}.` : `${group.name} is now ${roleLabel(role)} on ${resource?.name}.`);
    } catch (err) {
      if (err instanceof ApiError && err.requirement) setJustify({ target, requirement: err.requirement });
      else setError((err as Error).message);
    } finally { setBusy(false); }
  };
  return <Modal title={<>Give {group.name} access</>} wide onClose={onCancel}>
    <div className="segmented" role="group" aria-label="What to give">
      {([["object", "A role on an object"], ["product", "A data product"], ...(duties ? [["duty", "A review duty"]] : [])] as [typeof what, string][]).map(([key, label]) =>
        <button key={key} type="button" aria-pressed={what === key} className={what === key ? "active" : ""} onClick={() => { setWhat(key); setError(""); setJustify(null); }}>{label}</button>)}
    </div>
    {what === "duty" ? <DutyForm hub={hub} groupId={group.id} onDone={onDone} onCancel={onCancel} />
    : justify ? <JustifyPanel target={justify.target} requirement={justify.requirement} onCancel={() => setJustify(null)}
      onDone={(text) => { setJustify(null); onDone(text); }} /> : <form className="give-access" onSubmit={(e) => void give(e)}>
      {what === "product" ? <div className="arg-grid">
        <div className="field"><label htmlFor="give-product">Data product</label><select id="give-product" value={productId} onChange={(e) => { setProductId(e.target.value); setOption(""); }}>
          <option value="">Choose a product you own</option>{products.map((p) => <option key={p.id} value={p.id}>{p.title}{p.layer_name ? ` · ${p.layer_name}` : ""}</option>)}</select></div>
        {product && (product.options ?? []).length > 1 && <div className="field"><label htmlFor="give-option">Access option</label><select id="give-option" value={option} onChange={(e) => setOption(e.target.value)}>
          <option value="">The default option</option>{(product.options ?? []).map((o) => <option key={o.id} value={o.id}>{o.name} · {o.classification}</option>)}</select></div>}
      </div> : <>
        <div className="field"><label htmlFor="give-object">Object</label>
          <input type="search" aria-label="Find an object" placeholder="Search objects you own…" value={find} onChange={(e) => setFind(e.target.value)} />
          <select id="give-object" value={resourceId} onChange={(e) => {
            setResourceId(e.target.value); const r = objects.find((x) => x.id === e.target.value); setRole(r ? rolesFor(r).initial : ""); }}>
            <option value="">Choose an object you own</option>{shown.map((r) => <option key={r.id} value={r.id}>{resourcePath(r, hub.resources) || r.name}</option>)}</select></div>
        {resource && <RolePicker name="give-role" roles={roles} typeRoles={catalogRoles(catalog, catalogTypeKey(resource, hub.resources))} value={role} onChange={setRole} label={roleLabel} />}
      </>}
      <p className="hint">Only what you own is listed. When an access policy covers the data, you fill in its compliance form first and the access follows its review.</p>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="form-actions"><button className="btn" disabled={busy || (what === "product" ? !productId : !resourceId || !role)}>Give access</button>
        <button type="button" className="btn secondary" onClick={onCancel}>Cancel</button></div>
    </form>}
  </Modal>;
}

const SECTION_OF: Record<string, [string, string]> = {
  "set-layers": ["adm-platform", "Platform"], "set-pools": ["adm-platform", "Platform"], "set-runtimes": ["adm-platform", "Platform"],
  "set-backups": ["adm-platform", "Platform"], "set-template": ["adm-platform", "Platform"], "set-alerts": ["adm-platform", "Platform"],
  "set-profiles": ["adm-security", "Security"], "set-network": ["adm-security", "Security"],
  "set-classes": ["adm-privacy", "Privacy"], "set-policies": ["adm-privacy", "Privacy"], "set-forms": ["adm-privacy", "Privacy"], "set-duties": ["adm-privacy", "Privacy"],
  "set-groups": ["adm-access", "Access"], "set-services": ["adm-access", "Access"], "set-register": ["adm-access", "Access"],
  "set-auditlog": ["adm-access", "Access"], "set-querylog": ["adm-access", "Access"],
};

/** Who may see or change an Access or Administration page: Viewer or Manager, on this page, its section, or all of
 * Administration. Shown to whoever may give these roles (platform administrators). */
export function SettingsPermissions({ page, hub, openTab }: { page: string; hub: Hub; openTab: OpenTab }) {
  const [scope, setScope] = useState<"page" | "section" | "root">("page");
  const [section, sectionName] = SECTION_OF[page] ?? ["", ""];
  const id = scope === "page" ? page : scope === "section" ? section : "admin-root";
  const resource = hub.resources.find((r) => r.id === id);
  const self = hub.resources.find((r) => r.id === page);
  if (!self || !self.actions.includes("assign")) return null;
  return <details className="card settings-permissions">
    <summary>Who can see or change this page</summary>
    <p className="hint">Viewer may see it; Manager may also change it. Give a role on this page only, on all of {sectionName}, or on everything in Access and Administration.</p>
    <div className="segmented" role="group" aria-label="Grant on">
      {([["page", self.name], ["section", `All of ${sectionName}`], ["root", "Everything"]] as const).map(([value, label]) =>
        <button key={value} type="button" aria-pressed={scope === value} className={scope === value ? "active" : ""} onClick={() => setScope(value)}>{label}</button>)}
    </div>
    {resource && <PermissionsPanel key={resource.id} resource={resource} resources={hub.resources} openTab={openTab} onChanged={hub.refresh} />}
  </details>;
}


const ORIGIN: Record<GroupRoleAssignment["origin"], string> = { object: "Role on the object", product: "Through a data product", named: "Feature permission",
  custom: "Custom role", duty: "Review duty", system: "Protected" };
type ShownRole = GroupRoleAssignment & { features?: string[]; layers?: string[]; grant_ids?: string[] };
type ObjectGroup = { key: string; name: string; icon: string; resource?: AccessResource; roles: ShownRole[] };

const layerLabel = (a: GroupRoleAssignment) => !a.layer_id ? "Global" : a.layer_id === "*" ? "All layers" : a.layer_name ?? a.layer_id;

/** Feature permissions are given one per feature and layer. On one object, each role shows once, with the features
 * and layers it covers, instead of a row per grant. */
function mergeFeatureGrants(roles: GroupRoleAssignment[]): ShownRole[] {
  const out: ShownRole[] = [], merged = new Map<string, ShownRole>();
  for (const a of roles) {
    if (a.origin !== "named" && a.origin !== "custom") { out.push(a); continue; }
    const key = `${a.origin}:${a.role}`, seen = merged.get(key);
    if (!seen) {
      const first: ShownRole = { ...a, features: a.feature ? [a.feature] : [], layers: [layerLabel(a)], grant_ids: a.grant_id ? [a.grant_id] : [] };
      merged.set(key, first); out.push(first); continue;
    }
    seen.id += "+" + a.id;
    if (a.feature && !seen.features!.includes(a.feature)) seen.features!.push(a.feature);
    if (!seen.layers!.includes(layerLabel(a))) seen.layers!.push(layerLabel(a));
    if (a.grant_id) seen.grant_ids!.push(a.grant_id);
    const keys = new Set(seen.permissions.map((p) => p.key));
    seen.permissions = [...seen.permissions, ...a.permissions.filter((p) => !keys.has(p.key))];
  }
  return out;
}

/** A group's Has access to: every role it holds, under data, platform or privileged access; by object, each role
 * with the permissions it carries. One search covers objects, roles and permissions. */
export function GroupAccessInventory({ roles, group, hub, openTab, onChanged }: { roles: GroupRoleAssignment[]; group: { id: string; name: string; system?: boolean };
  hub: Hub; openTab: OpenTab; onChanged: (message: string) => void }) {
  const [query, setQuery] = useState("");
  const [closed, setClosed] = useState<Set<AccessCategory>>(new Set());
  const [openObjects, setOpenObjects] = useState<Set<string>>(new Set());
  const [openRoles, setOpenRoles] = useState<Set<string>>(new Set());
  const [giving, setGiving] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const q = query.trim().toLowerCase();
  const resourceOf = (id: string) => hub.resources.find((r) => r.id === id);
  const objectText = (a: GroupRoleAssignment) => { const r = resourceOf(a.target_id);
    return `${r ? resourcePath(r, hub.resources) : a.target_name} ${a.target_id} ${a.layer_name ?? ""}`.toLowerCase(); };
  const roleText = (a: GroupRoleAssignment) => `${a.role} ${roleLabel(a.role)} ${ORIGIN[a.origin]} ${a.product?.title ?? ""} ${a.description ?? ""} ${a.conditions.join(" ")}`.toLowerCase();
  const permissionMatches = (a: GroupRoleAssignment) => q ? a.permissions.filter((p) => `${p.key} ${p.description}`.toLowerCase().includes(q)) : [];
  // When searching, only matching roles show; a match on a permission opens its role and shows just those.
  const matched = useMemo(() => roles.filter((a) => !q || objectText(a).includes(q) || roleText(a).includes(q) || permissionMatches(a).length), [roles, q, hub.resources]);
  const permissionOnly = (a: GroupRoleAssignment) => !!q && !objectText(a).includes(q) && !roleText(a).includes(q);
  useEffect(() => {
    setOpenObjects(q ? new Set(matched.map((a) => a.target_id)) : new Set());
    setOpenRoles(q ? new Set(matched.filter(permissionOnly).map((a) => a.id)) : new Set());
  }, [q]);
  const toggle = <T,>(set: (f: (s: Set<T>) => Set<T>) => void, key: T) => set((s) => { const next = new Set(s); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const objectsOf = (category: AccessCategory) => {
    const groups = new Map<string, ObjectGroup>();
    for (const a of matched.filter((x) => x.category === category)) {
      const resource = resourceOf(a.target_id);
      const entry = groups.get(a.target_id) ?? { key: a.target_id, resource, roles: [],
        name: a.target_id === "*" ? "Everything" : resource ? resourcePath(resource, hub.resources) || resource.name : a.target_name,
        icon: String(resource?.config.source_kind ?? a.target_kind ?? (a.origin === "duty" ? "groups" : "layer")) };
      entry.roles.push(a); groups.set(a.target_id, entry);
    }
    return [...groups.values()].map((o) => ({ ...o, roles: mergeFeatureGrants(o.roles) }));
  };
  const all = CATEGORIES.flatMap((c) => objectsOf(c).map((o) => `${o.key}`));
  const remove = async (a: ShownRole) => {
    setBusy(true); setError("");
    try {
      if (a.origin === "duty") await api.compliance.removeRole(a.duty_id!);
      else if (a.origin === "named" || a.origin === "custom") for (const id of a.grant_ids ?? [a.grant_id!]) await api.access.removePermission(id);
      else await api.access.revoke(a.target_id, group.id, a.role);
      await hub.refresh(); onChanged(`Removed ${roleLabel(a.role)} on ${a.target_name} from ${group.name}.`);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const removable = (a: GroupRoleAssignment) => a.origin === "object" ? !!resourceOf(a.target_id)?.actions.includes("assign")
    : a.origin === "duty" ? can(hub.me, "compliance.duties.appoint", "set-duties")
    : (a.origin === "named" || a.origin === "custom") && !!a.grant_id && !!hub.me?.acting_admin;
  const rolesTab = (a: GroupRoleAssignment) => a.origin === "duty" ? { kind: "roles" as const, title: "Roles", ref: "duties", roleName: a.role }
    : a.origin === "custom" ? { kind: "roles" as const, title: "Roles", ref: "custom", roleName: a.role }
    : a.origin === "object" || a.origin === "product" ? { kind: "roles" as const, title: "Roles", roleName: a.role,
        ref: catalogTypeKey(resourceOf(a.target_id) ?? { id: a.target_id, kind: a.target_kind ?? "", parent_id: null }, hub.resources) } : null;

  const roleRow = (a: ShownRole) => { const open = openRoles.has(a.id), only = permissionOnly(a) ? permissionMatches(a) : a.permissions, link = rolesTab(a);
    return <li key={a.id} className="access-role">
      <div className="access-role-row">
        <Disclosure open={open} onToggle={() => toggle(setOpenRoles, a.id)} label={`${open ? "Hide" : "Show"} the permissions of ${roleLabel(a.role)} on ${a.target_name}`}>
          <span className="badge role-badge">{roleLabel(a.role)}</span>
          <span className="muted small">{a.permissions.length} permission{a.permissions.length === 1 ? "" : "s"}</span>
        </Disclosure>
        <span className="access-role-meta small">
          {a.origin === "product" && a.product ? <>Through <button className="text-button" onClick={() => openTab({ kind: "product", ref: a.product!.id, title: a.product!.title })}>{a.product.title}</button></>
            : a.features ? <>{a.features.length === 1 ? "Feature" : "Features"}: {a.features.join(", ")}</> : ORIGIN[a.origin]}
          {a.layers ? <> · {a.layers.join(", ")}{(a.grant_ids?.length ?? 0) > 1 && <span className="muted"> ({a.grant_ids!.length} grants, one per feature and layer)</span>}</>
            : a.layer_id && <> · {layerLabel(a)}</>}
          {a.description && <> · {a.description}</>}
          {a.conditions.map((c) => <span key={c} className={c.startsWith("Needs") ? "error-text" : "muted"}> · {c}</span>)}
        </span>
        <span className="row access-role-actions">
          {link && <button className="text-button small" onClick={() => openTab(link)}>About this role</button>}
          {removable(a) && <button className="btn sm secondary" disabled={busy} aria-label={`Remove ${roleLabel(a.role)} on ${a.target_name}`} onClick={() => void remove(a)}>Remove</button>}
        </span>
      </div>
      {open && <PermissionList rows={only} label={`Permissions of ${roleLabel(a.role)} on ${a.target_name}`} />}
    </li>; };

  return <div className="access-inventory">
    <div className="object-toolbar">
      <input type="search" className="access-search" aria-label="Search access" placeholder="Search objects, roles and permissions…" value={query} onChange={(e) => setQuery(e.target.value)} />
      <div className="row">
        <button className="text-button" onClick={() => { setClosed(new Set()); setOpenObjects(new Set(all)); }}>Expand all</button>
        <button className="text-button" onClick={() => { setOpenObjects(new Set()); setOpenRoles(new Set()); }}>Collapse all</button>
        {!group.system && <button className="btn" onClick={() => setGiving(true)}><IconPlus size={14} />Give access</button>}
      </div>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {CATEGORIES.map((category) => {
      const objects = objectsOf(category), count = objects.reduce((n, o) => n + o.roles.length, 0), open = !closed.has(category);
      // Counted as shown: feature grants of one role on one object are one row.
      const byObject = new Map<string, GroupRoleAssignment[]>();
      for (const r of roles.filter((x) => x.category === category)) byObject.set(r.target_id, [...(byObject.get(r.target_id) ?? []), r]);
      const total = [...byObject.values()].reduce((n, list) => n + mergeFeatureGrants(list).length, 0);
      return <section key={category} className={`access-category cat-${category}`} aria-label={CATEGORY_INFO[category].title}>
        <Disclosure open={open} onToggle={() => toggle(setClosed, category)} className="access-category-heading">
          <h3>{CATEGORY_INFO[category].title}</h3>
          <span className="badge">{q ? `${count} of ${total}` : total} role{(q ? count : total) === 1 ? "" : "s"}</span>
          <span className="muted small">{CATEGORY_INFO[category].description}</span>
        </Disclosure>
        {open && (!objects.length ? <p className="muted access-empty">{total ? "Nothing here matches." : "No roles of this kind."}</p>
          : <ul className="access-objects">{objects.map((o) => { const isOpen = openObjects.has(o.key);
            return <li key={o.key} className="access-object">
              <div className="access-object-row">
                <Disclosure open={isOpen} onToggle={() => toggle(setOpenObjects, o.key)} label={`${isOpen ? "Hide" : "Show"} roles on ${o.name}`}>
                  <ProductIcon kind={o.icon} size={16} /><span className="object-name">{o.name}</span>
                </Disclosure>
                <span className="row access-object-roles">{[...new Set(o.roles.map((a) => a.role))].map((r) => <span key={r} className="badge role-badge">{roleLabel(r)}</span>)}</span>
                {o.resource && <button className="btn sm secondary" onClick={() => openTab(resourceTab(o.resource!, "permissions"))}>Permissions</button>}
              </div>
              {isOpen && <ul className="access-roles">{o.roles.map(roleRow)}</ul>}
            </li>; })}</ul>)}
      </section>;
    })}
    <p className="hint">Across all layers. Roles on a parent object apply to everything below it, and grants add together. Approvals, data policies, the app profile and layer boundaries still apply when something runs.</p>
    {giving && <GiveAccess group={group} hub={hub} onCancel={() => setGiving(false)} onDone={(text) => { setGiving(false); onChanged(text); }} />}
  </div>;
}
