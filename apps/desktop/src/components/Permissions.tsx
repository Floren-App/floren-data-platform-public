import { useEffect, useState } from "react";
import { api, AccessResource, AppGroup, RoleAssignment, ServicePrincipal } from "../api";
import { ProductIcon } from "./ProductIcon";
import type { TabRef } from "../views/Tabs";
import { IconRefresh } from "./Icons";

export type ObjectSection = "overview" | "permissions" | "settings" | "preview" | "runs" | "sql" | "lineage" | "policies";
export type OpenTab = (tab: TabRef) => void;

export function resourcePath(resource: AccessResource, resources: AccessResource[]): string {
  return resourceChain(resource, resources).map((r) => r.name).join(" / ");
}

export function resourceChain(resource: AccessResource, resources: AccessResource[]): AccessResource[] {
  const chain = [resource];
  const seen = new Set([resource.id]);
  let parent = resources.find((r) => r.id === resource.parent_id);
  while (parent && !seen.has(parent.id)) {
    seen.add(parent.id); chain.unshift(parent);
    parent = resources.find((r) => r.id === parent?.parent_id);
  }
  return chain;
}

const API_KINDS = ["api-root", "api", "api-group", "api-operation"];

export function resourceTab(resource: AccessResource, section?: ObjectSection): TabRef {
  if (resource.kind === "kafka" || resource.kind === "topic") return { kind: "kafka", kafkaKind: resource.kind === "kafka" ? "cluster" : "topic", ref: resource.id, title: resource.name, section };
  if (resource.kind === "api-root") return { kind: "apis", title: "API management" };
  if (API_KINDS.includes(resource.kind)) {
    // Groups and operations open their API, focused on them; permissions live on the same pages.
    const apiId = resource.kind === "api" ? resource.object_id! : String(resource.config.api_id);
    return { kind: "api", ref: apiId, title: resource.kind === "api" ? resource.name : "API",
      apiSection: section === "permissions" && resource.kind === "api" ? "permissions" : "operations",
      apiFocus: resource.kind === "api" ? undefined : resource.id };
  }
  const kind = resource.kind === "source" ? "source" : resource.kind === "schema" ? "schema"
    : resource.kind === "service" ? resource.config.service_account ? "service" : "workload" : "folder";
  return { kind, ref: resource.kind === "source" ? resource.name
    : kind === "workload" ? resource.object_id! : resource.id, title: resource.name, section };
}

export function ObjectBreadcrumbs({ resource, resources, openTab, trailing = [] }: {
  resource: AccessResource; resources: AccessResource[]; openTab: OpenTab; trailing?: { name: string; onClick?: () => void }[];
}) {
  const chain = resourceChain(resource, resources);
  // A top-level object has nothing above it; its page title already names it.
  if (chain.length < 2 && !trailing.length) return null;
  return <nav className="breadcrumbs" aria-label="Object location">
    {chain.map((r, i, all) => <span key={r.id}>
      {i > 0 && <span className="breadcrumb-divider" aria-hidden="true">/</span>}
      {i === all.length - 1 && !trailing.length ? <span aria-current="page">{r.name}</span>
        : <button className="text-button" onClick={() => openTab(resourceTab(r))}>{r.name}</button>}
    </span>)}
    {trailing.map((part, i) => <span key={i}><span className="breadcrumb-divider" aria-hidden="true">/</span>
      {part.onClick ? <button className="text-button" onClick={part.onClick}>{part.name}</button> : <span aria-current={i === trailing.length - 1 ? "page" : undefined}>{part.name}</span>}
    </span>)}
  </nav>;
}

export function ObjectTabs({ active, onChange, settings = false, overview = "Overview", preview = false, runs = false, sql = false, lineage = false, policies = false }: {
  active: ObjectSection; onChange: (section: ObjectSection) => void; settings?: boolean; overview?: string; preview?: boolean; runs?: boolean; sql?: boolean; lineage?: boolean; policies?: boolean;
}) {
  const tabs: [ObjectSection, string][] = [["overview", overview], ["permissions", "Permissions"]];
  if (policies) tabs.push(["policies", "Masks and filters"]);
  if (runs) tabs.splice(1, 0, ["runs", "Recent runs"]);
  if (preview) tabs.splice(1, 0, ["preview", "Data preview"]);
  if (lineage) tabs.splice(1, 0, ["lineage", "Lineage"]);
  if (settings) tabs.push(["settings", "Settings"]);
  if (sql) tabs.splice(1, 0, ["sql", "SQL"]);
  return <nav className="object-tabs" aria-label="Object sections">
    {tabs.map(([id, label]) => <button key={id} aria-current={active === id ? "page" : undefined}
      className={active === id ? "active" : ""} onClick={() => onChange(id)}>{label}</button>)}
  </nav>;
}

const DATA_ROLES = {
  Reader: "Read data in this scope.",
  Contributor: "Read and write data. Cannot create or alter tables.",
  Admin: "Read, write, create, alter, and remove data objects. Cannot assign Floren Data Platform permissions.",
  Owner: "Full control, including assigning permissions to app groups.",
};
const API_ROLES = {
  Consumer: "Call the API's operations from programs, or try them out. Internal APIs are called with their own JWT or mTLS.",
  Contributor: "Call and change configuration, operations and endpoints.",
  Admin: "Create and remove operations and endpoint groups. Cannot assign permissions.",
  Owner: "Full control, including permissions and the API's managed identity.",
};
const PACKAGE_ROLES = {
  Publisher: "Publish new packages (their group becomes the package's Owner) and import packages from PyPI or CRAN.",
  Maintainer: "Also publish new versions of packages and yank versions.",
  Owner: "Full control, including deleting versions and assigning permissions.",
};
const PACKAGE_KINDS = ["package-root", "package"];
const PROGRAM_ROLES = {
  Executor: "Run programs and open apps.",
  Contributor: "Run and update existing programs and apps, including deployment.",
  Admin: "Create and remove programs, apps, and folders. Cannot assign permissions.",
  Owner: "Full control, including permissions and execution identity settings.",
};

/** Always scoped by the owning object; no global resource picker. */
export function PermissionsPanel(props: {
  resource: AccessResource; resources: AccessResource[]; openTab: OpenTab; onChanged: () => Promise<void>;
  initialServiceId?: string;
}) {
  return <PermissionEditor key={props.resource.id} {...props} />;
}

function PermissionEditor({ resource, resources, openTab, onChanged, initialServiceId }: Parameters<typeof PermissionsPanel>[0]) {
  const [assignments, setAssignments] = useState<RoleAssignment[]>([]);
  const [groups, setGroups] = useState<AppGroup[]>([]);
  const [services, setServices] = useState<ServicePrincipal[]>([]);
  const [users, setUsers] = useState<ServicePrincipal[]>([]);
  const [recipientType, setRecipientType] = useState(initialServiceId ? "service" : "group");
  const [serviceId, setServiceId] = useState(initialServiceId ?? "");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [reloadVersion, setReloadVersion] = useState(0);
  const [groupId, setGroupId] = useState("");
  const [search, setSearch] = useState("");
  const isKafka = resource.kind === "kafka" || resource.kind === "topic";
  const isData = resource.kind === "source" || resource.kind === "schema" || isKafka;
  const isApi = API_KINDS.includes(resource.kind);
  const isPackage = PACKAGE_KINDS.includes(resource.kind);
  const roles: Record<string, string> = isKafka ? { Reader: "Consume and commit offsets for own groups", Contributor: "Also produce messages", Admin: "Also create, alter and delete topics", Owner: "All topic permissions and role assignments" } : isData ? DATA_ROLES : isApi ? API_ROLES : isPackage ? PACKAGE_ROLES : PROGRAM_ROLES;
  const [role, setRole] = useState(isData ? "Reader" : isApi ? "Consumer" : isPackage ? (resource.kind === "package" ? "Maintainer" : "Publisher") : "Executor");
  const canRead = resource.actions.includes("read");
  const canAssign = resource.actions.includes("assign");

  useEffect(() => {
    let cancelled = false;
    setLoading(true); setError("");
    if (!canRead) { setLoading(false); return; }
    Promise.all([api.access.assignments(resource.id), canAssign ? api.access.groups() : Promise.resolve([]),
      canAssign ? api.access.servicePrincipals(resource.id) : Promise.resolve([])])
      .then(([a, g, s]) => { if (!cancelled) { setAssignments(a); setGroups(g); setServices(s); } })
      .catch((e: Error) => { if (!cancelled) setError(e.message); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [resource.id, canRead, canAssign, reloadVersion]);

  useEffect(() => {
    if (!canAssign || recipientType !== "user") return;
    let stale = false;
    api.access.userPrincipals(resource.id).then((rows) => { if (!stale) setUsers(rows); }).catch((e: Error) => { if (!stale) setError(e.message); });
    return () => { stale = true; };
  }, [resource.id, canAssign, recipientType]);

  useEffect(() => {
    if (initialServiceId) { setRecipientType("service"); setServiceId(initialServiceId); }
  }, [initialServiceId]);

  const change = async (fn: () => Promise<unknown>, success: string) => {
    setBusy(true); setError(""); setMessage("");
    try {
      await fn();
      // Refresh capabilities even when the change removes our own access.
      await onChanged();
      setAssignments(await api.access.assignments(resource.id)); setMessage(success);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const selectedGroup = groups.find((g) => g.id === groupId);
  const directPrincipals = recipientType === "user" ? users : services;
  const selectedService = directPrincipals.find((s) => s.id === serviceId);
  const recipientId = recipientType !== "group" ? serviceId : groupId;
  const recipientName = recipientType !== "group" ? selectedService?.name : selectedGroup?.name;
  const visibleGroups = groups.filter((g) => !g.system && (g.id === groupId || g.name.toLowerCase().includes(search.toLowerCase())));
  const visibleServices = directPrincipals.filter((s) => s.id === serviceId || s.name.toLowerCase().includes(search.toLowerCase()));
  const grantRows = (inherited: boolean) => assignments.filter((a) => a.inherited === inherited).map((a) => {
    const origin = resources.find((r) => r.id === a.resource_id);
    const name = a.principal_id ? a.principal_name ?? "Service identity" : a.group!;
    return <tr key={a.id}>
      <td>{a.principal_id ? <span className="row"><ProductIcon kind={a.principal_kind === "user" ? "user" : "identity"} size={16} />{name} <span className="badge">{a.principal_kind === "user" ? "User" : "Service"} · direct</span>
        {a.principal_active === false && <span className="badge">Inactive</span>}</span>
        : <button className="text-button" onClick={() => openTab({ kind: "access", ref: a.app_group_id!, title: name })}>{name}</button>}</td>
      <td><span className="badge role-badge">{a.role}</span></td>
      <td>{a.system ? "All objects" : inherited ? origin ? <button className="text-button" title={resourcePath(origin, resources)}
        onClick={() => openTab(resourceTab(origin, "permissions"))}>{resourcePath(origin, resources)}</button> : "Parent object"
        : resource.name}</td>
      <td className="table-actions">{!inherited && !a.system && canAssign && <button className="btn sm secondary" disabled={busy}
        aria-label={`Remove ${name}'s direct ${a.role} role`}
        onClick={() => change(() => a.principal_id ? api.access.revokeService(resource.id, a.principal_id) : api.access.revoke(resource.id, a.app_group_id!), `Removed the direct role for ${name}.`)}>Remove</button>}
        {a.system ? <span className="badge">System · protected</span> : inherited && <span className="badge">Inherited</span>}</td>
    </tr>;
  });

  return <section className="permissions-panel" aria-label={`Permissions for ${resource.name}`}>
    <div className="section-heading"><div><h2>Permissions <span className="badge">{resource.kind === "source" ? "Data source" : resource.kind === "schema" ? "Schema" : resource.kind === "root" ? "Workloads"
        : resource.kind === "api" ? "API" : resource.kind === "api-group" ? "Endpoint group" : resource.kind === "api-operation" ? "Operation" : resource.kind === "api-root" ? "All APIs" : resource.kind === "package-root" ? "Package repository" : resource.kind === "package" ? "Package" : resource.kind}</span></h2>
      <p className="muted">Who can access <strong>{resource.name}</strong>.</p></div>
      {canRead && <button className="btn secondary" disabled={busy || loading} onClick={() => { setMessage(""); setReloadVersion((v) => v + 1); }}><IconRefresh size={14} />Refresh permissions</button>}
    </div>
    {resource.owner_group_name && <p className="hint">Owner group · <strong>{resource.owner_group_name}</strong></p>}
    <p className="scope-note">{resource.kind === "source" ? "Grants apply to all registered schemas in this data source, including schemas registered later."
      : resource.kind === "schema" ? "Grants cover every current and future object in this schema."
      : resource.kind === "kafka" ? "Grants apply to every current and future topic in this Kafka cluster."
      : resource.kind === "topic" ? "Grants govern producing, consuming and managing this topic. Consumer groups are isolated by identity."
      : resource.kind === "folder" || resource.kind === "root" ? "Grants apply to this folder and every nested folder, program, and app."
      : resource.kind === "api-root" ? "Grants apply to every API, endpoint group and operation."
      : resource.kind === "api" ? "Grants apply to this API and all of its endpoint groups and operations."
      : resource.kind === "api-group" ? "Grants apply to every operation in this endpoint group."
      : resource.kind === "api-operation" ? "Grants apply to this operation only."
      : resource.kind === "package-root" ? "Everyone can install every package. Grants here decide who publishes and imports; they also apply to every package published here."
      : resource.kind === "package" ? "Who publishes new versions of this package and yanks versions. Everyone can install it."
      : "Grants control access to this service. Data access is granted separately on data sources and schemas."}
      {resource.parent_id && " Parent access always applies. Additional grants can be added here."}</p>
    {error && <p className="error" role="alert">{error}</p>}
    {message && <p className="success-message" role="status">{message}</p>}
    {!canRead ? <p className="muted">You can browse this object's children. Permission details require access to this object.</p>
      : loading ? <p className="muted" role="status">Loading permissions…</p> : <>
        <div className="table-scroll"><table>
          <thead><tr><th>App group or service</th><th>Role</th><th>Granted on</th><th /></tr></thead>
          <tbody>{grantRows(false)}{grantRows(true)}
            {!assignments.length && <tr><td colSpan={4} className="muted">No group or service grants. Platform administrators retain management access.</td></tr>}
          </tbody>
        </table></div>
        {canAssign ? <form className="card grant-form" style={{ maxWidth: 760 }} onSubmit={(e) => {
          e.preventDefault();
          if (!recipientId || busy) return;
          change(() => recipientType !== "group" ? api.access.assignService(resource.id, serviceId, role) : api.access.assign(resource.id, groupId, role), `Assigned ${role} to ${recipientName ?? "the recipient"}.`);
        }}>
          <h3>Assign a role</h3>
          <div className="field"><label htmlFor={`grant-type-${resource.id}`}>Grant to</label>
            <select id={`grant-type-${resource.id}`} value={recipientType} onChange={(e) => { setRecipientType(e.target.value); setServiceId(""); setSearch(""); }}>
              <option value="group">App group</option><option value="service">Service identity</option><option value="user">User</option>
            </select><span className="hint">Users and services can receive roles directly. App groups share access among their bound identities.</span>
          </div>
          <div className="arg-grid">
            <div className="field"><label htmlFor={`grant-search-${resource.id}`}>{recipientType !== "group" ? recipientType === "user" ? "Find a user" : "Find a service identity" : "Find an app group"}</label>
              <input id={`grant-search-${resource.id}`} type="search" placeholder={recipientType !== "group" ? recipientType === "user" ? "Search users…" : "Search services…" : "Search groups…"} value={search} onChange={(e) => setSearch(e.target.value)} />
              {recipientType !== "group" ? <select aria-label={recipientType === "user" ? "User" : "Service identity"} value={serviceId} onChange={(e) => setServiceId(e.target.value)}>
                <option value="">{recipientType === "user" ? "Choose user" : "Choose service identity"}</option>{visibleServices.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select> : <select aria-label="App group" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
                <option value="">Choose app group</option>{visibleGroups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
              </select>}
            </div>
            <div className="field"><label htmlFor={`grant-role-${resource.id}`}>Role</label>
              <select id={`grant-role-${resource.id}`} value={role} onChange={(e) => setRole(e.target.value)}>
                {Object.keys(roles).map((r) => <option key={r}>{r}</option>)}
              </select><span className="hint">{roles[role]}</span>
            </div>
          </div>
          {assignments.some((a) => !a.inherited && (recipientType !== "group" ? a.principal_id === serviceId : a.app_group_id === groupId)) && <p className="hint">This replaces the recipient's direct role. Inherited roles still apply.</p>}
          <div className="form-actions"><button className="btn" disabled={busy || !recipientId}>{busy ? "Saving…" : "Assign role"}</button>
            <button type="button" className="text-button" style={{ marginLeft: "auto" }} onClick={() => openTab({ kind: "access", title: "App groups" })}>Manage app groups</button></div>
        </form> : <p className="muted">An Owner can change permissions on this object. Inherited grants are managed on the parent.</p>}
        <details className="role-reference"><summary>What each role allows</summary>
          <dl className="kv">{Object.entries(roles).map(([name, description]) => <div className="role-description" key={name}>
            <dt>{name}</dt><dd>{description}</dd></div>)}</dl>
        </details>
      </>}
  </section>;
}
