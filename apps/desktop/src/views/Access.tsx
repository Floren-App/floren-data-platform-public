import { useCallback, useEffect, useState } from "react";
import { api, AccessResource, AppGroup, AppProfile, DirectoryPrincipal } from "../api";
import { resourcePath, resourceTab } from "../components/Permissions";
import type { OpenTab } from "../components/Permissions";
import type { TabRef } from "./Tabs";
import type { Hub } from "../hub";
import { ProductIcon } from "../components/ProductIcon";
import { OriginBadge } from "./Directory";
import { PageHeader } from "../components/Page";
import { IconPlus } from "../components/Icons";

/** Central IAM manages identities; resource permissions live on each object. */
export function AccessTab({ hub, groupId, bindingPrincipalId, onSelect, openTab }: { hub: Hub; groupId?: string; bindingPrincipalId?: string; onSelect: (id: string, name: string) => void; openTab: OpenTab }) {
  const [groups, setGroups] = useState<AppGroup[]>([]);
  const [directory, setDirectory] = useState<DirectoryPrincipal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState("");
  const [identitySearch, setIdentitySearch] = useState("");
  const [creating, setCreating] = useState(false);
  const [groupName, setGroupName] = useState("");
  const [description, setDescription] = useState("");
  const [owners, setOwners] = useState<string[]>([]);
  const [replacementOwners, setReplacementOwners] = useState<string[]>([]);
  const [principal, setPrincipal] = useState("");
  const [grants, setGrants] = useState<{ resource: AccessResource; role: string }[]>([]);

  const load = useCallback(async () => {
    const g = await api.access.groups();
    setGroups(g);
    if (hub.me?.is_admin || g.some((x) => x.can_manage)) setDirectory(await api.access.directory());
    else setDirectory([]);
  }, [hub.me?.is_admin]);
  useEffect(() => { load().catch((e: Error) => setError(e.message)).finally(() => setLoading(false)); }, [load]);
  const group = groups.find((g) => g.id === groupId);
  useEffect(() => {
    let cancelled = false; setGrants([]);
    if (group && group.can_inspect !== false) api.access.groupAssignments(group.id)
      .then((rows) => { if (!cancelled) setGrants(rows); }).catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [group?.id, group?.can_inspect]);
  useEffect(() => { setReplacementOwners(group?.owners ?? []); setPrincipal(bindingPrincipalId ?? ""); setIdentitySearch(""); }, [group?.id, group?.owners.join(","), bindingPrincipalId]);
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setError("");
    try { await fn(); await load(); await hub.refresh(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const userName = (id: string) => directory.find((p) => p.id === id)?.name ?? id;
  const matching = groups.filter((g) => `${g.name} ${g.description}`.toLowerCase().includes(search.toLowerCase()));
  return <>
    <PageHeader icon={<ProductIcon kind="groups" size={22} />} title="App groups"
      subtitle="App-managed security groups: assign them roles on schemas, objects, programs, and deployed folders, then bind users, IdP groups, roles, or services."
      actions={hub.me?.is_admin && <button className="btn" aria-expanded={creating} onClick={() => setCreating(true)}><IconPlus size={14} />Create app group</button>} />
    <p className="scope-note">Security groups represent who gets access. Workload folders organize programs and apps under Workloads, where each folder has its own Permissions tab.</p>
    {error && <p className="error" role="alert">{error}</p>}
    {creating && hub.me?.is_admin && <form className="card" onSubmit={(e) => { e.preventDefault(); act(async () => {
      const created = await api.access.createGroup({ name: groupName.trim(), description, owners });
      setCreating(false); setGroupName(""); setDescription(""); setOwners([]); onSelect(created.id, created.name);
    }); }}>
      <h3>Create app group</h3>
      <div className="arg-grid"><div className="field"><label htmlFor="group-name">Group name</label><input id="group-name" autoFocus required value={groupName} onChange={(e) => setGroupName(e.target.value)} /></div>
        <div className="field"><label htmlFor="group-desc">Description</label><input id="group-desc" value={description} onChange={(e) => setDescription(e.target.value)} /></div></div>
      <div className="field"><label htmlFor="group-owners">Designated owners</label>
        <select id="group-owners" multiple size={5} value={owners} onChange={(e) => setOwners(Array.from(e.target.selectedOptions, (o) => o.value))}>
          {directory.filter((p) => p.kind === "user" && p.active).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select><span className="hint">Owners can manage identity bindings. Only platform admins can change the designated owners. Hold ⌘/Ctrl to select several.</span></div>
      <div className="form-actions">
        <button className="btn" disabled={busy || !groupName.trim() || !owners.length}>Create group</button>
        <button type="button" className="btn secondary" onClick={() => setCreating(false)}>Cancel</button>
      </div>
    </form>}
    {loading ? <p className="muted">Loading groups…</p> : <div className="master-detail">
      <aside className="group-list" aria-label="App groups">
        <input type="search" aria-label="Search app groups" placeholder="Search app groups…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <ul>{matching.map((g) => <li key={g.id}><button className={groupId === g.id ? "active" : ""} onClick={() => onSelect(g.id, g.name)}>
          <strong className="row" style={{ gap: 6 }}><ProductIcon kind="groups" size={16} />{g.name}</strong>{g.description && <span className="muted">{g.description}</span>}
          <span className="muted">{g.can_manage ? "You manage this group" : g.can_inspect ? `${g.bindings.length} identity bindings` : "App group"}</span>
        </button></li>)}</ul>
        {!matching.length && <p className="muted">No matching app groups.</p>}
      </aside>
      <section className="group-detail" aria-label="Group details">
        {!group ? <div className="empty-state"><h2>{groupId ? "Group unavailable" : "Choose an app group"}</h2><p className="muted">Review its identity bindings and designated owners.</p></div> : <>
          <div className="section-heading"><h2 className="page-title"><ProductIcon kind="groups" />{group.name}</h2><OriginBadge /></div>
          {group.description && <p className="muted" style={{ marginTop: -4 }}>{group.description}</p>}
          {group.system && <p className="scope-note">Protected system group · Owner access to every current and future object. Object permissions cannot revoke this access. Workload layer boundaries still apply.</p>}
          {group.can_inspect !== false && <p className="muted">Owners: {group.owners.map(userName).join(", ") || "Platform administrators"}</p>}
          {!group.system && <GroupProfile group={group} hub={hub} act={act} openTab={openTab} />}
          {group.can_inspect !== false && <section><h3>Roles on objects</h3>
            <p className="hint">These roles are inherited by the object's children. Only resource Owners can change the assignments; group owners manage membership.</p>
            <table><thead><tr><th>Object</th><th>Role</th></tr></thead><tbody>{grants.map((grant) => <tr key={grant.resource.id}>
              <td><button className="text-button" onClick={() => openTab(resourceTab(grant.resource, "permissions"))}>{resourcePath(grant.resource, hub.resources)}</button></td><td><span className="badge role-badge">{grant.role}</span></td>
            </tr>)}{!grants.length && <tr><td colSpan={2} className="muted">No assignments on objects visible to you.</td></tr>}</tbody></table>
          </section>}
          {group.can_inspect === false ? <p className="muted">Identity bindings are visible to group members and owners.</p> : <>
            <h3>Identity bindings</h3>
            <div className="table-scroll"><table aria-label="Identity bindings"><thead><tr><th>Identity</th><th>Type</th><th /></tr></thead><tbody>
              {group.bindings.map((id) => { const p = directory.find((x) => x.id === id); return <tr key={id}>
                <td><span className="row" style={{ gap: 6, flexWrap: "wrap" }}><ProductIcon kind={p?.kind === "user" ? "users" : p?.kind === "service" ? "identity" : "groups"} size={16} />{p?.name ?? id}{p && !p.active && <span className="badge">Inactive</span>} {p && <OriginBadge principal={p} />}</span></td><td>{p?.kind ?? "identity"}</td>
                <td className="table-actions">{group.protected_bindings?.includes(id) ? <span className="badge">Protected</span> : group.can_manage && <button className="btn sm secondary" disabled={busy} aria-label={`Remove ${p?.name ?? id} from ${group.name}`}
                  onClick={() => act(() => api.access.unbind(group.id, id))}>Remove</button>}</td>
              </tr>; })}
              {!group.bindings.length && <tr><td colSpan={3} className="muted">No identity bindings yet.</td></tr>}
            </tbody></table></div>
          </>}
          {group.can_manage && <form className="card" onSubmit={(e) => { e.preventDefault(); act(async () => { await api.access.bind(group.id, principal); setPrincipal(""); }); }}>
            <h3>Bind an identity</h3>
            <p className="hint">Members receive this group's roles wherever it has access.</p>
            <div className="field"><label htmlFor="identity-search">Find an IdP identity or service</label>
              <input id="identity-search" type="search" placeholder="Search identities…" value={identitySearch} onChange={(e) => setIdentitySearch(e.target.value)} />
              <select aria-label="Identity to bind" required value={principal} onChange={(e) => setPrincipal(e.target.value)}>
                <option value="">Choose identity</option>
                {directory.filter((p) => p.active && (!group.system || p.kind !== "service") && !group.bindings.includes(p.id) && (p.id === principal || p.name.toLowerCase().includes(identitySearch.toLowerCase())))
                  .map((p) => <option key={p.id} value={p.id}>{p.name} ({p.kind})</option>)}
              </select></div>
            <div className="form-actions"><button className="btn" disabled={busy || !principal}>Bind identity</button></div>
          </form>}
          {hub.me?.is_admin && !group.system && <details className="card"><summary>Designated group owners</summary>
            <p className="muted">Changing owners leaves the group's identity bindings and assigned roles in place.</p>
            <div className="field"><label htmlFor="replacement-owners">Owners</label>
              <select id="replacement-owners" multiple size={5} value={replacementOwners} onChange={(e) => setReplacementOwners(Array.from(e.target.selectedOptions, (o) => o.value))}>
                {directory.filter((p) => p.active && p.kind === "user").map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select></div>
            <div className="form-actions"><button className="btn secondary" disabled={busy || !replacementOwners.length} onClick={() => act(() => api.access.updateGroup(group.id,
              { name: group.name, description: group.description, owners: replacementOwners }))}>Save owners</button></div>
          </details>}
        </>}
      </section>
    </div>}
    {directory.some((p) => p.kind === "group" || p.kind === "role") && <section className="card idp-groups"><h2 className="page-title"><ProductIcon kind="groups" />IdP groups &amp; roles</h2>
      <p className="hint">Synced identity groups are read-only here. Bind one to an app security group above to grant its members object permissions.</p>
      {directory.filter((p) => p.kind === "group" || p.kind === "role").map((p) => <details key={p.id}>
        <summary><ProductIcon kind="groups" size={16} />{p.name} <OriginBadge principal={p} /> <span className="muted">{p.kind} · {p.active ? "Active" : "Inactive"}</span></summary>
        <p className="muted">Provider: {p.provider || "Identity provider"}</p>
        <ul>{(p.members ?? []).map((id) => <li key={id}>{directory.find((d) => d.id === id)?.name ?? id}</li>)}</ul>
        {!p.members?.length && <p className="muted">No direct members.</p>}
      </details>)}
    </section>}
  </>;
}

/** Which parts of the app the group gets when it is someone's active group; administrators change it. */
function GroupProfile({ group, hub, act, openTab }: { group: AppGroup; hub: Hub; act: (fn: () => Promise<unknown>) => Promise<unknown>; openTab: (t: TabRef) => void }) {
  const [profiles, setProfiles] = useState<AppProfile[]>([]);
  useEffect(() => { api.appProfiles.list().then((r) => setProfiles(r.profiles)).catch(() => setProfiles([])); }, []);
  const fallback = profiles.find((p) => p.default)?.name;
  const current = group.app_profile ?? "";
  return <p className="row muted" style={{ gap: 8, flexWrap: "wrap" }}>App profile:
    {hub.me?.is_admin ? <select aria-label={`App profile of ${group.name}`} value={current}
        onChange={(e) => void act(() => api.access.updateGroup(group.id, { name: group.name, description: group.description, owners: group.owners, app_profile: e.target.value }))}>
        <option value="">Default ({fallback ?? "…"})</option>{profiles.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}</select>
      : <strong>{group.app_profile ?? `${fallback ?? "default"} (default)`}</strong>}
    {hub.me?.is_admin && <button className="text-button" onClick={() => openTab({ kind: "admin-app-profiles", title: "App profiles" })}>Edit profiles</button>}</p>;
}
