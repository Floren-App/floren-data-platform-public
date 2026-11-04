import { useEffect, useState } from "react";
import { AccessCategory, AccessResource, PermissionRow } from "../api";
import { PageHeader } from "../components/Page";
import { roleLabel, rolesFor } from "../components/Permissions";
import { ProductIcon } from "../components/ProductIcon";
import { CATEGORIES, CATEGORY_INFO, CategoryBadge, Disclosure, useRoleCatalog } from "../components/Roles";
import type { Hub } from "../hub";

// Where each kind of object sits in the list, and which object stands in for it when describing its roles.
const SECTIONS: [string, string[]][] = [
  ["Data", ["data-sql", "data-iceberg", "data-s3", "table", "file", "kafka", "topic", "product"]],
  ["Build", ["workloads", "service-account", "package"]],
  ["Integration", ["api-root", "api", "api-operation"]],
];
const SAMPLE: Record<string, { kind: string; icon: string }> = {
  "data-sql": { kind: "schema", icon: "postgres" }, "data-iceberg": { kind: "schema", icon: "iceberg" }, "data-s3": { kind: "schema", icon: "bucket" },
  table: { kind: "table", icon: "table" }, file: { kind: "file", icon: "file" }, kafka: { kind: "kafka", icon: "kafka" }, topic: { kind: "topic", icon: "topic" },
  product: { kind: "product", icon: "data" }, workloads: { kind: "folder", icon: "folder" }, "service-account": { kind: "service", icon: "identity" },
  package: { kind: "package", icon: "python" }, "api-root": { kind: "api-root", icon: "api" }, api: { kind: "api", icon: "api" }, "api-operation": { kind: "api-operation", icon: "api" },
};

type Role = { key: string; name: string; label: string; category: AccessCategory; description: string; permissions: string[] };

/** Access → Roles: permissions belong to roles, roles are given to groups, people are members of groups. Every role
 * each kind of object offers, what kind of access it is and every permission in it. */
export function RolesView({ typeKey, roleName }: { hub: Hub; typeKey?: string; roleName?: string }) {
  const catalog = useRoleCatalog();
  const [selected, setSelected] = useState(typeKey ?? "data-sql");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set(roleName ? [`${typeKey}/${roleName}`] : []));
  useEffect(() => { if (roleName) document.getElementById(`role-${typeKey}/${roleName}`)?.scrollIntoView({ block: "center" }); }, [catalog, typeKey, roleName]);
  if (!catalog) return <><Header /><p className="muted loading" role="status">Loading roles…</p></>;

  const settings = catalog.types.filter((t) => t.family === "settings");
  const entries: { key: string; label: string; icon: string; roles: Role[]; note?: string }[] = [
    ...catalog.types.map((t) => {
      const sample = SAMPLE[t.key] ?? { kind: "setting", icon: "layer" };
      const descriptions = rolesFor({ kind: sample.kind as AccessResource["kind"] }).roles;
      return { key: t.key, label: t.label, icon: sample.icon, note: t.pages ? `Covers ${t.pages.join(", ")}. A role on one page carries only that page's part.` : undefined,
        roles: t.roles.map((r) => ({ key: `${t.key}/${r.name}`, name: r.name, label: roleLabel(r.name), category: r.category,
          description: descriptions[r.name] ?? "", permissions: r.permissions })) };
    }),
    { key: "custom", label: "Custom roles", icon: "identity", note: "Named sets of permissions, declared in Terraform (fdp_custom_role) and given like any role.",
      roles: catalog.custom_roles.map((r) => ({ key: `custom/${r.name}`, name: r.name, label: r.name, category: r.category, description: "", permissions: r.permissions })) },
    { key: "duties", label: "Review duties", icon: "groups", note: "Deciding who gets access. Given to a group on its Has access to page, scoped to layers, classifications or groups.",
      roles: catalog.review_duties.map((d) => ({ key: `duties/${d.name}`, name: d.name, label: d.name, category: d.category, description: d.description, permissions: d.permissions })) },
  ];
  const q = query.trim().toLowerCase();
  const permissionText = (key: string) => `${key} ${catalog.permissions[key]?.description ?? ""}`.toLowerCase();
  const roleMatches = (r: Role) => !q || `${r.name} ${r.label} ${r.description}`.toLowerCase().includes(q);
  const matchingPermissions = (r: Role) => q ? r.permissions.filter((p) => permissionText(p).includes(q)) : [];
  const visibleRoles = (e: typeof entries[number]) => e.roles.filter((r) => roleMatches(r) || matchingPermissions(r).length);
  const entry = entries.find((e) => e.key === selected) ?? entries[0];
  const toggle = (key: string) => setOpen((s) => { const next = new Set(s); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  const item = (e: typeof entries[number]) => { const count = visibleRoles(e).length;
    return (!q || count > 0) && (e.roles.length > 0 || e.key !== "custom") && <li key={e.key}><button className={e.key === entry.key ? "active" : ""} aria-current={e.key === entry.key ? "true" : undefined} onClick={() => setSelected(e.key)}>
      <strong className="row" style={{ gap: 6 }}><ProductIcon kind={e.icon} size={16} />{e.label}</strong>
      <span className="muted">{q ? `${count} matching role${count === 1 ? "" : "s"}` : `${e.roles.length} role${e.roles.length === 1 ? "" : "s"}`}</span></button></li>; };

  return <>
    <Header />
    <div className="object-toolbar"><input type="search" aria-label="Search roles and permissions" placeholder="Search roles and permissions…" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
    <div className="master-detail">
      <aside className="group-list" aria-label="Kinds of objects">
        {[...SECTIONS.map(([title, keys]) => [title, keys.map((k) => entries.find((e) => e.key === k)!).filter(Boolean)] as const),
          ["Administration", entries.filter((e) => settings.some((t) => t.key === e.key))] as const,
          ["Other", entries.filter((e) => e.key === "custom" || e.key === "duties")] as const].map(([title, list]) => {
          const items = list.map(item).filter(Boolean);
          return !!items.length && <div key={title}><div className="section-title">{title}</div><ul>{items}</ul></div>; })}
      </aside>
      <section className="group-detail" aria-label={`Roles of ${entry.label}`}>
        <h2 className="page-title"><ProductIcon kind={entry.icon} />{entry.label}</h2>
        {entry.note && <p className="hint">{entry.note}</p>}
        {!entry.roles.length && <p className="muted">None yet.</p>}
        {CATEGORIES.map((category) => { const roles = visibleRoles(entry).filter((r) => r.category === category);
          return !!roles.length && <section key={category} className="role-category" aria-label={CATEGORY_INFO[category].title}>
            <h3><CategoryBadge category={category} /> {CATEGORY_INFO[category].title}</h3>
            <p className="hint">{CATEGORY_INFO[category].description}</p>
            <ul className="role-list">{roles.map((r) => { const matched = matchingPermissions(r);
              const isOpen = open.has(r.key) || (matched.length > 0 && !roleMatches(r));
              const shown = matched.length && !roleMatches(r) ? matched : r.permissions;
              return <li key={r.key} id={`role-${r.key}`} className={r.key === `${typeKey}/${roleName}` ? "highlight" : ""}>
                <Disclosure open={isOpen} onToggle={() => toggle(r.key)} label={`${isOpen ? "Hide" : "Show"} permissions of ${r.label}`}>
                  <strong>{r.label}</strong><span className="muted small">{r.permissions.length} permission{r.permissions.length === 1 ? "" : "s"}</span>
                </Disclosure>
                {r.description && <p className="hint role-summary">{r.description}</p>}
                {isOpen && <PermissionList rows={shown.map((key) => ({ key, ...(catalog.permissions[key] ?? { description: key, category: "platform", constraint: "" }) }))} label={`Permissions of ${r.label}`} />}
              </li>; })}</ul>
          </section>; })}
        {q && !visibleRoles(entry).length && <p className="muted">No roles here match. Choose another kind of object on the left.</p>}
      </section>
    </div>
  </>;
}

function Header() {
  return <PageHeader icon={<ProductIcon kind="identity" size={22} />} title="Roles"
    subtitle="Permissions belong to roles. Roles are given to groups on an object or a page, and people and service identities are members of groups. Roles on a parent object apply to everything below it." />;
}

/** A role's permissions, the strongest kind first, each with its own kind of access. */
export function PermissionList({ rows, label }: { rows: PermissionRow[]; label: string }) {
  const rank = (p: PermissionRow) => CATEGORIES.indexOf(p.category);
  const sorted = [...rows].sort((a, b) => rank(a) - rank(b) || a.key.localeCompare(b.key));
  return <div className="table-scroll"><table className="permission-list" aria-label={label}><tbody>
    {sorted.map((p) => <tr key={p.key}>
      <td>{p.description}<div className="muted small"><code>{p.key}</code></div></td>
      <td><CategoryBadge category={p.category} /></td>
      <td className="muted small">{p.constraint}</td></tr>)}
  </tbody></table></div>;
}
