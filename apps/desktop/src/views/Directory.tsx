import { useState } from "react";
import { api, DirectoryPrincipal } from "../api";
import type { Hub } from "../hub";
import { useCached } from "../cache";
import type { OpenTab } from "../components/Permissions";
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
  { kind: "access", title: "App groups", icon: "groups" },
  { kind: "users", title: "Users", icon: "users" },
  { kind: "services", title: "Services", icon: "identity" },
  { kind: "secrets", title: "Secrets", icon: "key" },
] as const;
export type SecurityPage = typeof SECURITY_PAGES[number]["kind"];

export function SecurityIcon({ icon, size = 16 }: { icon: typeof SECURITY_PAGES[number]["icon"]; size?: number }) {
  return icon === "key" ? <span className="product-icon" style={{ color: "#d6b344" }}><IconKey size={size} /></span> : <ProductIcon kind={icon} size={size} />;
}

export function IAMNavigation({ active, openTab }: { active: SecurityPage; openTab: OpenTab }) {
  return <nav className="iam-navigation" aria-label="Security pages">
    {SECURITY_PAGES.map((item) =>
      <button key={item.kind} aria-current={active === item.kind ? "page" : undefined} className={active === item.kind ? "active" : ""} onClick={() => openTab({ kind: item.kind, title: item.title })}>
        <SecurityIcon icon={item.icon} />{item.title}
      </button>)}
  </nav>;
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
    <PageHeader icon={<ProductIcon kind="users" size={22} />} title="Users"
      subtitle="Users and IdP membership are provisioned by your identity provider. App security groups connect these identities to object permissions." />
    {!allowed ? <p className="scope-note">Platform admins and designated group owners can inspect the directory.</p> : <>
      {directory.error && <p className="error" role="alert">{directory.error}</p>}
      <div className="object-toolbar"><input aria-label="Find user" type="search" placeholder="Search users…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <button className="btn secondary" disabled={directory.status === "loading"} onClick={directory.refresh}><IconRefresh size={14} />Refresh users</button></div>
      {directory.status === "loading" ? <p className="muted" role="status">Loading users…</p> : <table><thead><tr><th>User</th><th>Identity source</th><th>Status</th><th>Platform role</th></tr></thead><tbody>
        {users.map((p) => <tr key={p.id} className={p.id === selectedId ? "selected" : ""}><td><button className="text-button object-name" onClick={() => openTab({ kind: "users", ref: p.id, title: p.name })}><ProductIcon kind="users" size={16} />{p.name}</button>
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
        <h3>App security groups</h3><p className="hint">Effective membership includes bindings through IdP groups and roles.</p>
        <ul className="object-links">{(selected.app_groups ?? []).map((id) => <li key={id}>
          <ProductIcon kind="groups" size={16} /><button className="text-button" onClick={() => openTab({ kind: "access", ref: id, title: groups.data?.find((g) => g.id === id)?.name ?? "App group" })}>{groups.data?.find((g) => g.id === id)?.name ?? id}</button>
        </li>)}</ul>{!selected.app_groups?.length && <p className="muted">No visible app-group memberships.</p>}
      </section>}
    </>}
  </>;
}
