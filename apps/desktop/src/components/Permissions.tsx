import { useEffect, useState } from "react";
import { api, AccessResource, ApiError, AppGroup, CaseTarget, JustifyDeadline, Requirement, RoleAssignment, ServicePrincipal } from "../api";
import { JustifyPanel } from "./ComplianceForm";
import { ProductIcon } from "./ProductIcon";
import type { TabRef } from "../views/Tabs";
import { IconPlus, IconRefresh } from "./Icons";
import { CATEGORIES, CATEGORY_INFO, CategoryBadge, catalogRoles, catalogTypeKey, Modal, RolePicker, useRoleCatalog } from "./Roles";

export type ObjectSection = "overview" | "permissions" | "settings" | "preview" | "runs" | "sql" | "lineage" | "policies" | "versions" | "logs" | "alerts";
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
  if (["table", "column", "file", "directory"].includes(resource.kind))
    return { kind:"data-permissions", ref:resource.id, title:resource.name, section:section ?? "permissions" };
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

export function roleLabel(role: string): string {
  return ({DataReader:"Data Reader",DataContributor:"Data Contributor",AccessAdministrator:"Access Administrator"} as Record<string,string>)[role] || role;
}

/** The roles an object offers (with what each allows), and the one to suggest first. Reader and Contributor see and
 * configure the object; Data Reader and Data Contributor work with what it holds; Executor only triggers runs. */
export function rolesFor(resource: Pick<AccessResource, "kind">) {
  const kind: string = resource.kind;
  const isDataObject = ["table", "column", "file", "directory"].includes(kind);
  const isKafka = kind === "kafka" || kind === "topic";
  const isData = kind === "source" || kind === "schema" || isKafka || isDataObject;
  const isApi = API_KINDS.includes(kind);
  const isPackage = PACKAGE_KINDS.includes(kind);
  const isProduct = kind === "product";
  const roles: Record<string, string> = ["setting", "settings-section", "settings-root"].includes(kind) ? PAGE_ROLES
    : isProduct ? PRODUCT_ROLES : isKafka ? KAFKA_ROLES : isData ? DATA_ROLES : isApi ? API_ROLES : isPackage ? PACKAGE_ROLES
    : kind === "service" ? SERVICE_ROLES : PROGRAM_ROLES;
  const initial = isData || isProduct || isApi || isPackage ? "DataReader" : roles.Executor ? "Executor" : "Reader";
  return { roles, initial, isData, isProduct, isApi, isPackage, isKafka };
}

export function ObjectTabs({ active, onChange, settings = false, overview = "Overview", preview = false, runs = false, sql = false, lineage = false, policies = false, versions = false, logs = false, alerts = false }: {
  active: ObjectSection; onChange: (section: ObjectSection) => void; settings?: boolean; overview?: string; preview?: boolean; runs?: boolean; sql?: boolean; lineage?: boolean; policies?: boolean; versions?: boolean; logs?: boolean; alerts?: boolean;
}) {
  const tabs: [ObjectSection, string][] = [["overview", overview], ["permissions", "Permissions"]];
  if (logs) tabs.splice(1, 0, ["logs", "Logs"]);
  if (versions) tabs.splice(1, 0, ["versions", "Versions"]);
  if (policies) tabs.push(["policies", "Masks and filters"]);
  if (runs) tabs.splice(1, 0, ["runs", "Recent runs"]);
  if (preview) tabs.splice(1, 0, ["preview", "Data preview"]);
  if (lineage) tabs.splice(1, 0, ["lineage", "Lineage"]);
  if (alerts) tabs.push(["alerts", "Alerts"]);
  if (settings) tabs.push(["settings", "Settings"]);
  if (sql) tabs.splice(1, 0, ["sql", "SQL"]);
  return <nav className="object-tabs" aria-label="Object sections">
    {tabs.map(([id, label]) => <button key={id} aria-current={active === id ? "page" : undefined}
      className={active === id ? "active" : ""} onClick={() => onChange(id)}>{label}</button>)}
  </nav>;
}

const PRIVILEGED = {
  AccessAdministrator: "Give and take away roles here, without reading data or changing configuration.",
  Admin: "Everything except giving access: configure, use, delete.",
  Owner: "Full control, including giving access to others.",
};
const DATA_ROLES = {
  Reader: "See the object, its tables and columns and its configuration. No data, no preview, no access from code.",
  Contributor: "Change settings and descriptive metadata, and create or alter tables. No data.",
  DataReader: "Read the data: SELECT, data preview and reading files from code.",
  DataContributor: "Read and change the data: insert, update, delete and write files.",
  ...PRIVILEGED,
};
const KAFKA_ROLES = {
  Reader: "See the topic and its configuration. No messages.",
  Contributor: "Configure the topic: partitions, retention. No messages.",
  DataReader: "Subscribe: consume messages and commit offsets for your own consumer groups.",
  DataContributor: "Publish and consume messages.",
  ...PRIVILEGED,
};
const API_ROLES = {
  Reader: "See the API and its operations.",
  Contributor: "Configure the API: endpoints, groups and operations. No calls.",
  DataReader: "Call its GET, HEAD and OPTIONS operations.",
  DataContributor: "Call all its operations, also POST, PUT, PATCH and DELETE (SOAP operations are POST).",
  ...PRIVILEGED,
};
const PACKAGE_ROLES = {
  Reader: "See packages and versions.",
  Contributor: "Manage availability: yank versions and the import allowlist.",
  DataReader: "Download and install packages.",
  DataContributor: "Also publish new packages (the group becomes their Owner) and import from PyPI or CRAN. New versions of a package are its Owner's.",
  ...PRIVILEGED,
};
const PACKAGE_KINDS = ["package-root", "package"];
const PRODUCT_ROLES = {
  Viewer: "Find the product in the catalog and ask for access. Nothing else.",
  Reader: "See the product and its configuration, without its data.",
  Contributor: "Change the product's description and members, without its data.",
  DataReader: "Use everything in the product: read its tables and topics, open its dashboards, call its APIs' read operations.",
  ...PRIVILEGED,
};
const PROGRAM_ROLES = {
  Reader: "See workloads, their settings and status.",
  Contributor: "Change settings, deploy and create folders. No running.",
  Executor: "Trigger on-demand workloads (scripts, notebooks, dbt) and see their runs.",
  DataReader: "Open apps and documentation, and read run outputs and logs.",
  ...PRIVILEGED,
};
const SERVICE_ROLES = {
  Reader: "See the service identity and its configuration.",
  Contributor: "Change its settings.",
  Executor: "Run things as this identity.",
  ...PRIVILEGED,
};
const PAGE_ROLES = {
  Reader: "See this page and its configuration.",
  Contributor: "Change what this page configures.",
  DataReader: "Also read the records it holds: log contents, query text, exports.",
  ...PRIVILEGED,
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
  const { roles, initial } = rolesFor(resource);
  const [role, setRole] = useState(initial);
  const canRead = resource.capabilities ? resource.capabilities.includes("permissions.bindings.read") : resource.actions.includes("assign");
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
    if (initialServiceId) { setRecipientType("service"); setServiceId(initialServiceId); setAdding(true); }
  }, [initialServiceId]);

  const [justify, setJustify] = useState<{ target: CaseTarget; requirement: Requirement } | null>(null);
  const [adding, setAdding] = useState(!!initialServiceId);
  const catalog = useRoleCatalog();
  const typeKey = catalogTypeKey(resource, resources);
  const typeRoles = catalogRoles(catalog, typeKey);
  const categoryOf = (r: string) => typeRoles?.find((x) => x.name === r)?.category;
  // Access that needs a form and has none yet: it stays until its deadline.
  const [deadlines, setDeadlines] = useState<JustifyDeadline[]>([]);
  useEffect(() => { if (canAssign) api.compliance.deadlines(resource.id).then(setDeadlines).catch(() => setDeadlines([])); }, [resource.id, canAssign]);
  const justifyExisting = (d: JustifyDeadline) => { setError(""); setMessage("");
    const target: CaseTarget = { kind: "role", resource_id: resource.id, role: d.role, app_group_id: d.app_group_id };
    api.compliance.requirement(target).then((requirement) => setJustify({ target, requirement })).catch((e: Error) => setError(e.message)); };
  const change = async (fn: () => Promise<unknown>, success: string) => {
    setBusy(true); setError(""); setMessage(""); setJustify(null);
    try {
      await fn();
      // Refresh capabilities even when the change removes our own access.
      await onChanged();
      setAssignments(await api.access.assignments(resource.id)); setMessage(success); setAdding(false);
    } catch (e) {
      // Data under an access policy: the role is given once a compliance form is approved.
      if (e instanceof ApiError && e.requirement && recipientType === "group" && groupId)
        setJustify({ target: { kind: "role", resource_id: resource.id, role, app_group_id: groupId }, requirement: e.requirement });
      setError((e as Error).message);
    }
    finally { setBusy(false); }
  };
  const selectedGroup = groups.find((g) => g.id === groupId);
  const directPrincipals = recipientType === "user" ? users : services;
  const selectedService = directPrincipals.find((s) => s.id === serviceId);
  const recipientId = recipientType !== "group" ? serviceId : groupId;
  const recipientName = recipientType !== "group" ? selectedService?.name : selectedGroup?.name;
  const visibleGroups = groups.filter((g) => !g.system && (g.id === groupId || g.name.toLowerCase().includes(search.toLowerCase())));
  const visibleServices = directPrincipals.filter((s) => s.id === serviceId || s.name.toLowerCase().includes(search.toLowerCase()));
  const grantRow = (a: RoleAssignment) => {
    const inherited = a.inherited;
    const origin = resources.find((r) => r.id === a.resource_id);
    const name = a.principal_id ? a.principal_name ?? "Service identity" : a.group!;
    return <tr key={a.id}>
      <td>{a.principal_id ? <span className="row"><ProductIcon kind={a.principal_kind === "user" ? "user" : "identity"} size={16} />{name} <span className="badge">{a.principal_kind === "user" ? "User" : "Service"} · direct</span>
        {a.principal_active === false && <span className="badge">Inactive</span>}</span>
        : <button className="text-button" onClick={() => openTab({ kind: "access", ref: a.app_group_id!, title: name })}>{name}</button>}</td>
      <td><button className="text-button role-link" title="See this role's permissions" onClick={() => openTab({ kind: "roles", title: "Roles", ref: typeKey, roleName: a.role })}>
        <span className="badge role-badge">{roleLabel(a.role)}</span></button>
        {!inherited && (() => { const d = deadlines.find((x) => x.app_group_id === a.app_group_id); return d && <div className="small">
          <span className="error-text">Needs a form by {new Date(d.due_at).toLocaleDateString()}</span>{" "}
          <button className="text-button" disabled={busy} onClick={() => justifyExisting(d)}>Justify</button></div>; })()}</td>
      <td>{a.system ? "All objects" : inherited ? origin ? <button className="text-button" title={resourcePath(origin, resources)}
        onClick={() => openTab(resourceTab(origin, "permissions"))}>{resourcePath(origin, resources)}</button> : "Parent object"
        : resource.name}</td>
      <td className="table-actions">{!inherited && !a.system && canAssign && <button className="btn sm secondary" disabled={busy}
        aria-label={`Remove ${name}'s direct ${a.role} role`}
        onClick={() => change(() => a.principal_id ? api.access.revokeService(resource.id, a.principal_id, a.role) : api.access.revoke(resource.id, a.app_group_id!, a.role), `Removed ${roleLabel(a.role)} for ${name}.`)}>Remove</button>}
        {a.system ? <span className="badge">System · protected</span> : inherited && <span className="badge">Inherited</span>}</td>
    </tr>;
  };
  // Direct grants first, then inherited ones; grouped by what kind of access the role gives.
  const ordered = [...assignments].sort((a, b) => Number(a.inherited) - Number(b.inherited));
  const grouped = typeRoles ? CATEGORIES.map((c) => [c, ordered.filter((a) => (categoryOf(a.role) ?? "platform") === c)] as const).filter(([, rows]) => rows.length) : [];
  const roleNames = Object.keys(roles);

  return <section className="permissions-panel" aria-label={`Permissions for ${resource.name}`}>
    <div className="section-heading"><div><h2>Permissions <span className="badge">{resource.kind === "source" ? "Data source" : resource.kind === "schema" ? "Schema" : resource.kind === "root" ? "Workloads"
        : resource.kind === "api" ? "API" : resource.kind === "api-group" ? "Endpoint group" : resource.kind === "api-operation" ? "Operation" : resource.kind === "api-root" ? "All APIs" : resource.kind === "package-root" ? "Package repository" : resource.kind === "package" ? "Package" : resource.kind === "product" ? "Data product" : resource.kind}</span></h2>
      <p className="muted">Who can access <strong>{resource.name}</strong>, and with which role.</p></div>
      <div className="row">
        {canRead && <button className="btn secondary" disabled={busy || loading} onClick={() => { setMessage(""); setReloadVersion((v) => v + 1); }}><IconRefresh size={14} />Refresh permissions</button>}
        {canRead && canAssign && <button className="btn" disabled={loading} onClick={() => { setMessage(""); setError(""); setJustify(null); setAdding(true); }}><IconPlus size={14} />Add role</button>}
      </div>
    </div>
    {resource.owner_group_name && <p className="hint">Owner group · <strong>{resource.owner_group_name}</strong></p>}
    <p className="scope-note">{resource.kind === "product" ? "Data Reader gives the use of everything in this product, also what is added later. Removing a group, an object or the product ends that access; grants on the objects themselves are separate."
      : resource.kind === "source" ? "Grants apply to all registered schemas in this data source, including schemas registered later."
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
    {error && !justify && !adding && <p className="error" role="alert">{error}</p>}
    {justify && !adding && <JustifyPanel target={justify.target} requirement={justify.requirement} onCancel={() => setJustify(null)}
      onDone={(text) => { setJustify(null); setError(""); setMessage(text); }} />}
    {message && <p className="success-message" role="status">{message}</p>}
    {!canRead ? <p className="muted">You can browse this object's children. Permission bindings require explicit inspection permission.</p>
      : loading ? <p className="muted" role="status">Loading permissions…</p> : <>
        <div className="table-scroll"><table className="grants-table">
          <thead><tr><th>App group or service</th><th>Role</th><th>Granted on</th><th /></tr></thead>
          {grouped.length ? grouped.map(([category, rows]) => <tbody key={category} className={`cat-${category}`} aria-label={CATEGORY_INFO[category].title}>
            <tr className="category-row"><th colSpan={4}><span className="cat-name">{CATEGORY_INFO[category].title}</span>
              <span className="muted small"> · {CATEGORY_INFO[category].description}</span></th></tr>
            {rows.map(grantRow)}</tbody>)
            : <tbody>{ordered.map(grantRow)}
              {!assignments.length && <tr><td colSpan={4} className="muted">No group or service grants. Platform administrators retain management access.</td></tr>}</tbody>}
        </table></div>
        {!canAssign && <p className="muted">An Owner or Access Administrator can change permissions on this object. Inherited grants are managed on the parent.</p>}
        {adding && canAssign && <Modal title={<>Add a role on {resource.name}</>} wide onClose={() => { setAdding(false); setJustify(null); setError(""); }}>
          {justify ? <JustifyPanel target={justify.target} requirement={justify.requirement} onCancel={() => setJustify(null)}
            onDone={(text) => { setJustify(null); setError(""); setAdding(false); setMessage(text); }} />
          : <form className="grant-form" aria-label="Add a role" onSubmit={(e) => {
            e.preventDefault();
            if (!recipientId || busy) return;
            change(() => recipientType !== "group" ? api.access.assignService(resource.id, serviceId, role) : api.access.assign(resource.id, groupId, role), `Assigned ${roleLabel(role)} to ${recipientName ?? "the recipient"}.`);
          }}>
            <div className="arg-grid">
              <div className="field"><label htmlFor={`grant-type-${resource.id}`}>Grant to</label>
                <select id={`grant-type-${resource.id}`} value={recipientType} onChange={(e) => { setRecipientType(e.target.value); setServiceId(""); setSearch(""); }}>
                  <option value="group">App group</option><option value="service">Service identity</option><option value="user">User</option>
                </select><span className="hint">Groups share access among their members. Users and services can also get a role directly.</span>
              </div>
              <div className="field"><label htmlFor={`grant-search-${resource.id}`}>{recipientType !== "group" ? recipientType === "user" ? "Find a user" : "Find a service identity" : "Find an app group"}</label>
                <input id={`grant-search-${resource.id}`} type="search" placeholder={recipientType !== "group" ? recipientType === "user" ? "Search users…" : "Search services…" : "Search groups…"} value={search} onChange={(e) => setSearch(e.target.value)} />
                {recipientType !== "group" ? <select aria-label={recipientType === "user" ? "User" : "Service identity"} value={serviceId} onChange={(e) => setServiceId(e.target.value)}>
                  <option value="">{recipientType === "user" ? "Choose user" : "Choose service identity"}</option>{visibleServices.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select> : <select aria-label="App group" value={groupId} onChange={(e) => setGroupId(e.target.value)}>
                  <option value="">Choose app group</option>{visibleGroups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                </select>}
              </div>
            </div>
            <RolePicker name={`grant-role-${resource.id}`} roles={roles} typeRoles={typeRoles} value={role} onChange={setRole} label={roleLabel} />
            {assignments.some((a) => !a.inherited && (recipientType !== "group" ? a.principal_id === serviceId : a.app_group_id === groupId)) && <p className="hint">Roles add up: the recipient keeps the roles it has here.</p>}
            {error && <p className="error" role="alert">{error}</p>}
            <div className="form-actions"><button className="btn" disabled={busy || !recipientId || !role}>{busy ? "Saving…" : "Assign role"}</button>
              <button type="button" className="btn secondary" onClick={() => setAdding(false)}>Cancel</button>
              <button type="button" className="text-button" style={{ marginLeft: "auto" }} onClick={() => openTab({ kind: "roles", title: "Roles", ref: typeKey, roleName: role })}>What does this role allow?</button></div>
          </form>}
        </Modal>}
        <details className="role-reference"><summary>What each role allows</summary>
          {(typeRoles ? CATEGORIES : [null]).map((category) => { const list = roleNames.filter((r) => !category || categoryOf(r) === category);
            return !!list.length && <div key={category ?? "all"} className="role-reference-group">
              {category && <h4><CategoryBadge category={category} /> {CATEGORY_INFO[category].title}</h4>}
              <dl className="kv">{list.map((name) => <div className="role-description" key={name}>
                <dt>{roleLabel(name)}</dt><dd>{roles[name]}</dd></div>)}</dl></div>; })}
          <button className="text-button" onClick={() => openTab({ kind: "roles", title: "Roles", ref: typeKey })}>See every permission of these roles</button>
        </details>
      </>}
  </section>;
}
