import { useEffect, useState } from "react";
import { api, DirectoryPrincipal } from "../api";
import type { Hub } from "../hub";
import { useCached } from "../cache";
import type { OpenTab } from "../components/Permissions";
import { ComplianceRolesHeld, HasAccessTo } from "./AccessArea";
import { ProductIcon } from "../components/ProductIcon";
import { IconKey, IconRefresh } from "../components/Icons";
import { PageHeader } from "../components/Page";

export function OriginBadge({ principal }: { principal?: DirectoryPrincipal }) {
  const app = !principal || principal.origin === "app" || principal.kind === "service" || principal.provider === "local";
  return <span className={`badge origin-badge ${app ? "app-origin" : "idp-origin"}`} title={app ? "Created and managed by Floren Data Platform" : principal?.provider}>
    {app ? "App-managed" : `IdP · ${principal?.provisioning === "scim" || principal?.managed ? "SCIM" : "OIDC"}`}
  </span>;
}

/** The Security area's pages, in the order the sidebar lists them. */
export const SECURITY_PAGES = [
  { kind: "users", title: "People", icon: "users" },
  { kind: "access", title: "Groups", icon: "groups" },
  { kind: "services", title: "Service identities", icon: "identity" },
  { kind: "secrets", title: "Secrets", icon: "key" },
] as const;
export type SecurityPage = typeof SECURITY_PAGES[number]["kind"];

export function SecurityIcon({ icon, size = 16 }: { icon: typeof SECURITY_PAGES[number]["icon"]; size?: number }) {
  return icon === "key" ? <span className="product-icon" style={{ color: "#d6b344" }}><IconKey size={size} /></span> : <ProductIcon kind={icon} size={size} />;
}

export function UsersTab({ hub, selectedId, openTab }: { hub: Hub; selectedId?: string; openTab: OpenTab }) {
  const groups = useCached("catalog:groups", api.access.groups);
  const allowed = !!hub.me?.is_admin || !!groups.data?.some((g) => g.can_manage);
  const directory = useCached("catalog:directory", api.access.directory, allowed);
  const [search, setSearch] = useState("");
  const principals = directory.data ?? [];
  const users = principals.filter((p) => p.kind === "user" && `${p.name} ${p.display_name ?? ""}`.toLowerCase().includes(search.toLowerCase()));
  const selected = principals.find((p) => p.id === selectedId && p.kind === "user");
  return <>
    <PageHeader icon={<ProductIcon kind="users" size={22} />} title="People"
      subtitle="People and their IdP groups come from your identity provider. Groups connect them to roles on objects; a person's page shows what they can reach." />
    {!allowed ? <p className="scope-note">Platform admins and designated group owners can inspect the directory.</p> : <>
      {directory.error && <p className="error" role="alert">{directory.error}</p>}
      <div className="object-toolbar"><input aria-label="Find user" type="search" placeholder="Search users…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <button className="btn secondary" disabled={directory.status === "loading"} onClick={directory.refresh}><IconRefresh size={14} />Refresh users</button></div>
      {directory.status === "loading" ? <p className="muted" role="status">Loading users…</p> : <table><thead><tr><th>User</th><th>Identity source</th><th>Status</th><th>Platform role</th></tr></thead><tbody>
        {users.map((p) => <tr key={p.id} className={p.id === selectedId ? "selected" : ""}><td><button className="text-button object-name" onClick={() => openTab({ kind: "users", ref: p.id, title: p.name })} aria-label={`Open ${p.name}`}><ProductIcon kind="users" size={16} />{p.name}</button>
          {p.display_name && p.display_name !== p.name && <div className="muted">{p.display_name}</div>}</td>
          <td><OriginBadge principal={p} /></td><td><span className="status"><span className={`dot ${p.active ? "running" : ""}`} />{p.active ? "Active" : "Inactive"}</span></td><td>{p.platform_admin ? "Platform admin" : "User"}</td></tr>)}
        {!users.length && <tr><td colSpan={4} className="muted">No matching users. Users appear after IdP provisioning.</td></tr>}
      </tbody></table>}
      {selected && <section className="card"><h2 className="page-title"><ProductIcon kind="users" />{selected.name} <OriginBadge principal={selected} /></h2>
        <dl className="kv"><dt>Status</dt><dd>{selected.active ? "Active" : "Inactive"}</dd><dt>Platform role</dt><dd>{selected.platform_admin ? "Platform admin" : "User"}</dd>
          <dt>Identity provider</dt><dd className="mono">{selected.provider}</dd><dt>External ID</dt><dd className="mono">{selected.external_id}</dd></dl>
        <h3>IdP groups &amp; roles</h3><p className="hint">Membership is read-only here and maintained through the IdP.</p>
        <ul className="object-links">{(selected.member_of ?? []).map((id) => {
          const p = principals.find((item) => item.id === id);
          return <li key={id}><ProductIcon kind="groups" size={16} />{p?.name ?? id} <OriginBadge principal={p} /></li>;
        })}</ul>{!selected.member_of?.length && <p className="muted">No direct IdP memberships.</p>}
        <PersonAccess id={selected.id} hub={hub} openTab={openTab} />
      </section>}
      {principals.some((p) => p.kind === "group" || p.kind === "role") && <details className="card idp-groups"><summary>IdP groups &amp; roles</summary>
        <p className="hint">Synced from your identity provider and read-only here. Make one a member of a group to give its people that group's roles.</p>
        {principals.filter((p) => p.kind === "group" || p.kind === "role").map((p) => <details key={p.id}>
          <summary><ProductIcon kind="groups" size={16} />{p.name} <OriginBadge principal={p} /> <span className="muted">{p.kind} · {p.active ? "Active" : "Inactive"}</span></summary>
          <ul>{(p.members ?? []).map((id) => <li key={id}>{principals.find((d) => d.id === id)?.name ?? id}</li>)}</ul>
          {!p.members?.length && <p className="muted">No direct members.</p>}
        </details>)}
      </details>}
    </>}
  </>;
}

/** A person's groups (and through what), what those give them, and their compliance roles. */
function PersonAccess({ id, hub, openTab }: { id: string; hub: Hub; openTab: OpenTab }) {
  const [data, setData] = useState<Awaited<ReturnType<typeof api.access.principalAccess>> | null>(null), [error, setError] = useState("");
  useEffect(() => { setData(null); setError(""); api.access.principalAccess(id).then(setData).catch((e: Error) => setError(e.message)); }, [id]);
  if (error) return <p className="muted">{error.includes("administrators") ? "What someone can reach is visible to administrators and to the person." : error}</p>;
  if (!data) return <p className="muted" role="status">Loading…</p>;
  return <>
    <h3>Groups</h3>
    <ul className="object-links">{data.groups.map((g) => <li key={g.id}><ProductIcon kind="groups" size={16} />
      <button className="text-button" onClick={() => openTab({ kind: "access", ref: g.id, title: g.name })}>{g.name}</button>
      {g.via && <span className="muted"> · through {g.via}</span>}{g.system && <span className="badge">Owner on everything</span>}</li>)}</ul>
    {!data.groups.length && <p className="muted">Not in any group.</p>}
    <h3>Has access to</h3>
    <HasAccessTo rows={data.grants} hub={hub} openTab={openTab} showGroup label={`What ${data.principal.name} can reach`} />
    <ComplianceRolesHeld roles={data.compliance_roles} openTab={openTab} showGroup />
  </>;
}
