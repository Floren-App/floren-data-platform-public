import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { AccessCategory, AccessResource, api, CatalogRole, RoleCatalog } from "../api";
import { IconChevron, IconClose } from "./Icons";

/** Permissions belong to roles, roles are given to groups, people are members of groups. Every role falls in one
 * of three categories; these are the words for them everywhere in the app. */
export const CATEGORIES: AccessCategory[] = ["data", "platform", "privileged"];
export const CATEGORY_INFO: Record<AccessCategory, { title: string; short: string; description: string }> = {
  data: { title: "Data access", short: "Data", description: "Read or change actual data, files and messages." },
  platform: { title: "Platform access", short: "Platform", description: "See pages and objects, use features and change configuration." },
  privileged: { title: "Privileged access", short: "Privileged", description: "Administer and delete objects, review access requests and give access to others." },
};

export function CategoryBadge({ category }: { category: AccessCategory }) {
  return <span className={`badge cat-badge cat-${category}`}>{CATEGORY_INFO[category].short}</span>;
}

let catalogRequest: Promise<RoleCatalog | null> | null = null;
/** The roles each kind of object offers, with their category and permissions. Loaded once per session. */
export function useRoleCatalog(): RoleCatalog | null {
  const [catalog, setCatalog] = useState<RoleCatalog | null>(null);
  useEffect(() => {
    let stale = false;
    catalogRequest ??= api.access.roleCatalog().catch(() => { catalogRequest = null; return null; });
    void catalogRequest.then((c) => { if (!stale) setCatalog(c); });
    return () => { stale = true; };
  }, []);
  return catalog;
}

/** Which entry of the role catalog an object's roles come from. */
export function catalogTypeKey(resource: { id: string; kind: string; parent_id: string | null; config?: Record<string, unknown> }, resources: AccessResource[]): string {
  const kind = resource.kind;
  if (kind === "source" || kind === "schema") {
    const source = kind === "source" ? resource : resources.find((r) => r.id === resource.parent_id);
    const sourceKind = String(source?.config?.source_kind ?? "");
    return sourceKind === "s3" ? "data-s3" : sourceKind === "iceberg" ? "data-iceberg" : "data-sql";
  }
  if (kind === "table" || kind === "column") return "table";
  if (kind === "file" || kind === "directory") return "file";
  if (kind === "kafka" || kind === "topic" || kind === "api-root" || kind === "api") return kind;
  if (kind === "api-group" || kind === "api-operation") return "api-operation";
  if (kind === "service" && resource.config?.service_account) return "service-account";
  if (kind === "package-root" || kind === "package") return "package";
  if (kind === "product" || kind === "product-root") return "product";
  if (kind === "settings-root") return "settings:admin-root";
  if (kind === "settings-section") return `settings:${resource.id}`;
  if (kind === "setting") return `settings:${resource.parent_id}`;
  return "workloads";
}

export function catalogRoles(catalog: RoleCatalog | null, typeKey: string): CatalogRole[] | null {
  return catalog?.types.find((t) => t.key === typeKey)?.roles ?? null;
}

// The order roles are offered in, within their category: the narrow ones first.
const ORDER = ["DataReader", "DataContributor", "Viewer", "Reader", "Contributor", "Executor", "AccessAdministrator", "Admin", "Owner"];
const byOrder = (a: string, b: string) => (ORDER.indexOf(a) + 1 || 99) - (ORDER.indexOf(b) + 1 || 99);

/** The roles of one object, grouped by category; each with what it allows. */
export function RolePicker({ name, roles, typeRoles, value, onChange, label = (r: string) => r }: {
  name: string; roles: Record<string, string>; typeRoles: CatalogRole[] | null; value: string; onChange: (role: string) => void; label?: (role: string) => string;
}) {
  const info = (role: string) => typeRoles?.find((r) => r.name === role);
  const names = Object.keys(roles).sort(byOrder);
  const current = names;
  const option = (role: string) => <label key={role} className="role-option">
    <input type="radio" name={name} value={role} checked={value === role} onChange={() => onChange(role)} />
    <span><strong>{label(role)}</strong><span className="hint">{roles[role]}</span></span>
  </label>;
  const groups = typeRoles ? CATEGORIES.map((c) => [c, current.filter((r) => info(r)?.category === c)] as const).filter(([, list]) => list.length) : [];
  const ungrouped = typeRoles ? current.filter((r) => !info(r)) : current;
  return <fieldset className="role-picker">
    <legend>Role</legend>
    {groups.map(([category, list]) => <div key={category} className={`role-group cat-${category}`} role="group" aria-label={CATEGORY_INFO[category].title}>
      <div className="role-group-title"><CategoryBadge category={category} /><span className="muted small">{CATEGORY_INFO[category].description}</span></div>
      {list.map(option)}
    </div>)}
    {!!ungrouped.length && <div className="role-group">{ungrouped.map(option)}</div>}
  </fieldset>;
}

/** A dialog over the page, for adding things without crowding the page with forms. Escape or Close cancels. */
export function Modal({ title, onClose, children, wide = false }: { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    return () => dialog?.close();
  }, []);
  return <dialog ref={ref} className={`modal${wide ? " wide" : ""}`} aria-labelledby={id} onCancel={(e) => { e.preventDefault(); onClose(); }}>
    <header className="modal-header"><h2 id={id}>{title}</h2>
      <button type="button" className="icon-btn" aria-label="Close" onClick={onClose}><IconClose /></button></header>
    <div className="modal-body">{children}</div>
  </dialog>;
}

/** A button that shows or hides what follows it. */
export function Disclosure({ open, onToggle, label, children, className = "" }: { open: boolean; onToggle: () => void; label?: string; children: ReactNode; className?: string }) {
  return <button type="button" className={`disclosure ${className}`} aria-expanded={open} aria-label={label} onClick={onToggle}>
    <span className={`disclosure-chevron${open ? " open" : ""}`}><IconChevron size={12} /></span>{children}</button>;
}
