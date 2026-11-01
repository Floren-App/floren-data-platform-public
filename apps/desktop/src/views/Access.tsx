import { useCallback, useEffect, useState } from "react";
import { api, AppGroup, AppProfile, DirectoryPrincipal } from "../api";
import { ComplianceRolesHeld, GiveAccess, HasAccessTo } from "./AccessArea";
import { PrivacyRoles } from "./Privacy";
import type { OpenTab } from "../components/Permissions";
import type { TabRef } from "./Tabs";
import type { Hub } from "../hub";
import { ProductIcon } from "../components/ProductIcon";
import { OriginBadge } from "./Directory";
import { PageHeader } from "../components/Page";
import { IconPlus } from "../components/Icons";

type GroupSection = "members" | "access" | "settings";

/** Access → Groups: who is in each group, what it can reach, and its settings. Permissions per object stay on
 * the object's Permissions tab. */
export function AccessTab({ hub, groupId, section, bindingPrincipalId, onSelect, openTab }: { hub: Hub; groupId?: string; section: GroupSection;
  bindingPrincipalId?: string; onSelect: (id: string, name: string, section?: GroupSection) => void; openTab: OpenTab }) {
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
  const [access, setAccess] = useState<Awaited<ReturnType<typeof api.access.groupAccess>> | null>(null);
  const [name, setName] = useState(""), [desc, setDesc] = useState("");
  const [newProfile, setNewProfile] = useState(""), [newMembers, setNewMembers] = useState<string[]>([]);
  const [profiles, setProfiles] = useState<AppProfile[]>([]), [accessVersion, setAccessVersion] = useState(0), [notice, setNotice] = useState("");
  useEffect(() => { if (creating) api.appProfiles.list().then((r) => setProfiles(r.profiles)).catch(() => setProfiles([])); }, [creating]);

  const load = useCallback(async () => {
    const g = await api.access.groups();
    setGroups(g);
    if (hub.me?.is_admin || g.some((x) => x.can_manage)) setDirectory(await api.access.directory());
    else setDirectory([]);
  }, [hub.me?.is_admin]);
  useEffect(() => { load().catch((e: Error) => setError(e.message)).finally(() => setLoading(false)); }, [load]);
  const group = groups.find((g) => g.id === groupId);
  useEffect(() => {
    let cancelled = false; setAccess(null);
    if (group && group.can_inspect !== false && section === "access") api.access.groupAccess(group.id)
      .then((rows) => { if (!cancelled) setAccess(rows); }).catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [group?.id, group?.can_inspect, section, accessVersion]);
  useEffect(() => { setName(group?.name ?? ""); setDesc(group?.description ?? ""); }, [group?.id, group?.name, group?.description]);
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
    <PageHeader icon={<ProductIcon kind="groups" size={22} />} title="Groups"
      subtitle="Groups get roles on objects (on each object's Permissions tab) and an app profile; people, IdP groups and service identities are their members."
      actions={hub.me?.is_admin && <button className="btn" aria-expanded={creating} onClick={() => setCreating(true)}><IconPlus size={14} />Create group</button>} />
    {error && <p className="error" role="alert">{error}</p>}
    {creating && hub.me?.is_admin && <form className="card" aria-label="New group" onSubmit={(e) => { e.preventDefault(); act(async () => {
      // One page from start to finish: the group, what its people see, and who is in it; then what it can reach.
      const created = await api.access.createGroup({ name: groupName.trim(), description, owners });
      if (newProfile) await api.access.updateGroup(created.id, { name: created.name, description, owners, app_profile: newProfile });
      for (const id of newMembers) await api.access.bind(created.id, id);
      setCreating(false); setGroupName(""); setDescription(""); setOwners([]); setNewProfile(""); setNewMembers([]);
      onSelect(created.id, created.name, "access");
    }); }}>
      <h3>New group</h3>
      <div className="arg-grid"><div className="field"><label htmlFor="group-name">Group name</label><input id="group-name" autoFocus required value={groupName} onChange={(e) => setGroupName(e.target.value)} /></div>
        <div className="field"><label htmlFor="group-desc">Description</label><input id="group-desc" value={description} onChange={(e) => setDescription(e.target.value)} /></div></div>
      <div className="field"><label htmlFor="group-owners">Designated owners</label>
        <select id="group-owners" multiple size={5} value={owners} onChange={(e) => setOwners(Array.from(e.target.selectedOptions, (o) => o.value))}>
          {directory.filter((p) => p.kind === "user" && p.active).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select><span className="hint">Owners manage the members. Only platform administrators change the owners. Hold ⌘/Ctrl to select several.</span></div>
      <div className="field"><label htmlFor="group-profile">What its people see (app profile)</label>
        <select id="group-profile" value={newProfile} onChange={(e) => setNewProfile(e.target.value)}>
          <option value="">{profiles.find((p) => p.default) ? `The default (${profiles.find((p) => p.default)!.name})` : "The whole app"}</option>
          {profiles.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}</select></div>
      <div className="field"><label htmlFor="group-members">Members</label>
        <select id="group-members" multiple size={6} value={newMembers} onChange={(e) => setNewMembers(Array.from(e.target.selectedOptions, (o) => o.value))}>
          {directory.filter((p) => p.active).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.kind === "user" ? "person" : p.kind === "service" ? "service identity" : `IdP ${p.kind}`})</option>)}
        </select><span className="hint">People, IdP groups and service identities; you can add more later. Next you choose what the group can reach.</span></div>
      <div className="form-actions">
        <button className="btn" disabled={busy || !groupName.trim() || !owners.length}>Create group</button>
        <button type="button" className="btn secondary" onClick={() => setCreating(false)}>Cancel</button>
      </div>
    </form>}
    {loading ? <p className="muted">Loading groups…</p> : <div className="master-detail">
      <aside className="group-list" aria-label="Groups">
        <input type="search" aria-label="Search groups" placeholder="Search groups…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <ul>{matching.map((g) => <li key={g.id}><button className={groupId === g.id ? "active" : ""} onClick={() => onSelect(g.id, g.name, section)}>
          <strong className="row" style={{ gap: 6 }}><ProductIcon kind="groups" size={16} />{g.name}</strong>{g.description && <span className="muted">{g.description}</span>}
          <span className="muted">{g.can_manage ? "You manage this group" : g.can_inspect ? `${g.bindings.length} member${g.bindings.length === 1 ? "" : "s"}` : "Group"}</span>
        </button></li>)}</ul>
        {!matching.length && <p className="muted">No matching groups.</p>}
      </aside>
      <section className="group-detail" aria-label="Group details">
        {!group ? <div className="empty-state"><h2>{groupId ? "Group unavailable" : "Choose a group"}</h2><p className="muted">See its members, what it can reach and its settings.</p></div> : <>
          <div className="section-heading"><h2 className="page-title"><ProductIcon kind="groups" />{group.name}</h2><OriginBadge /></div>
          {group.description && <p className="muted" style={{ marginTop: -4 }}>{group.description}</p>}
          {group.system && <p className="scope-note">Protected system group · Owner access to every current and future object. Object permissions cannot revoke this access. Workload layer boundaries still apply.</p>}
          {group.can_inspect !== false && <p className="muted">Owners: {group.owners.map(userName).join(", ") || "Platform administrators"}</p>}
          {group.can_inspect !== false && <nav className="object-tabs" aria-label="Group sections">
            {([["members", "Members"], ["access", "Has access to"], ["settings", "Settings"]] as [GroupSection, string][]).map(([id, label]) =>
              <button key={id} className={section === id ? "active" : ""} aria-current={section === id ? "page" : undefined} onClick={() => onSelect(group.id, group.name, id)}>{label}</button>)}
          </nav>}
          {section === "access" && group.can_inspect !== false && <>
            {group.system && <p className="scope-note">Owner on every object, including what is added later.</p>}
            {!access ? <p className="muted" role="status">Loading…</p> : <>
              <p className="hint">Roles on objects, directly and through data products. Change them on the object's Permissions tab.
                {access.app_profile ? <> App profile: <strong>{access.app_profile}</strong>.</> : null}</p>
              {notice && <p className="success-message" role="status">{notice}</p>}
              <HasAccessTo rows={access.grants} hub={hub} openTab={openTab} />
              <ComplianceRolesHeld roles={access.compliance_roles} openTab={openTab} />
              {!group.system && <GiveAccess key={group.id} group={group} hub={hub} onDone={(text) => { setNotice(text); setAccessVersion((v) => v + 1); }} />}
            </>}
          </>}
          {section === "settings" && group.can_inspect !== false && <>
            {!group.system && <GroupProfile group={group} hub={hub} act={act} openTab={openTab} />}
            {!group.system && (hub.me?.is_admin || hub.me?.compliance_roles?.includes("compliance_admin")) && <PrivacyRoles hub={hub} groupId={group.id} />}
            {hub.me?.is_admin && !group.system && <form className="card" onSubmit={(e) => { e.preventDefault(); void act(() => api.access.updateGroup(group.id, { name: name.trim(), description: desc, owners: group.owners })); }}>
              <h3>Name and description</h3>
              <div className="arg-grid"><div className="field"><label htmlFor="group-rename">Name</label><input id="group-rename" required value={name} onChange={(e) => setName(e.target.value)} /></div>
                <div className="field"><label htmlFor="group-redesc">Description</label><input id="group-redesc" value={desc} onChange={(e) => setDesc(e.target.value)} /></div></div>
              <div className="form-actions"><button className="btn secondary" disabled={busy || !name.trim() || (name === group.name && desc === group.description)}>Save</button></div>
            </form>}
          </>}
          {group.can_inspect === false ? <p className="muted">Members are visible to group members and owners.</p> : section === "members" && <>
            <h3>Members</h3>
            <p className="hint">People, IdP groups and roles, and service identities. Members get this group's roles wherever it has access.</p>
            <div className="table-scroll"><table aria-label="Members"><thead><tr><th>Identity</th><th>Type</th><th /></tr></thead><tbody>
              {group.bindings.map((id) => { const p = directory.find((x) => x.id === id); return <tr key={id}>
                <td><span className="row" style={{ gap: 6, flexWrap: "wrap" }}><ProductIcon kind={p?.kind === "user" ? "users" : p?.kind === "service" ? "identity" : "groups"} size={16} />{p?.name ?? id}{p && !p.active && <span className="badge">Inactive</span>} {p && <OriginBadge principal={p} />}</span></td><td>{p?.kind ?? "identity"}</td>
                <td className="table-actions">{group.protected_bindings?.includes(id) ? <span className="badge">Protected</span> : group.can_manage && <button className="btn sm secondary" disabled={busy} aria-label={`Remove ${p?.name ?? id} from ${group.name}`}
                  onClick={() => act(() => api.access.unbind(group.id, id))}>Remove</button>}</td>
              </tr>; })}
              {!group.bindings.length && <tr><td colSpan={3} className="muted">No members yet.</td></tr>}
            </tbody></table></div>
          </>}
          {group.can_manage && section === "members" && <form className="card" onSubmit={(e) => { e.preventDefault(); act(async () => { await api.access.bind(group.id, principal); setPrincipal(""); }); }}>
            <h3>Add a member</h3>
            <div className="field"><label htmlFor="identity-search">Find an IdP identity or service</label>
              <input id="identity-search" type="search" placeholder="Search identities…" value={identitySearch} onChange={(e) => setIdentitySearch(e.target.value)} />
              <select aria-label="Identity to bind" required value={principal} onChange={(e) => setPrincipal(e.target.value)}>
                <option value="">Choose identity</option>
                {directory.filter((p) => p.active && (!group.system || p.kind !== "service") && !group.bindings.includes(p.id) && (p.id === principal || p.name.toLowerCase().includes(identitySearch.toLowerCase())))
                  .map((p) => <option key={p.id} value={p.id}>{p.name} ({p.kind})</option>)}
              </select></div>
            <div className="form-actions"><button className="btn" disabled={busy || !principal}>Add member</button></div>
          </form>}
          {hub.me?.is_admin && !group.system && section === "settings" && <details className="card" open><summary>Group owners</summary>
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
        <option value="">Default ({fallback ?? "the whole app"})</option>{profiles.map((p) => <option key={p.id} value={p.name}>{p.name}</option>)}</select>
      : <strong>{group.app_profile ?? (fallback ? `${fallback} (default)` : "The whole app (no profile)")}</strong>}
    {hub.me?.is_admin && <button className="text-button" onClick={() => openTab({ kind: "admin-app-profiles", title: "App profiles" })}>Edit profiles</button>}</p>;
}
