import type { Me } from "./api";
import type { TabKind } from "./views/Tabs";

export const TAB_PAGES: Record<TabKind, string> = {
  "home": "home",
  "catalog": "catalog",
  "catalog-object": "catalog",
  "products": "products",
  "product": "products",
  "access-requests": "requests",
  "users": "people",
  "access": "set-groups",
  "workspaces": "workspaces",
  "workspace": "workspaces",
  "runs": "runs",
  "run": "runs",
  "data": "storage",
  "source": "storage",
  "schema": "storage",
  "data-object": "storage",
  "admin-sources": "storage",
  "kafka": "kafka",
  "kafka-clusters": "kafka",
  "apis": "apis",
  "api": "apis",
  "pool": "usage",
  "folder": "workloads",
  "deploy": "workloads",
  "workload": "workloads",
  "service": "set-services",
  "services": "set-services",
  "admin-service-accounts": "set-services",
  "admin-layers": "set-layers",
  "app-profiles": "set-profiles",
  "admin-app-profiles": "set-profiles",
  "admin-notifications": "set-alerts",
  "admin-classifications": "set-classes",
  "privacy-classifications": "set-classes",
  "privacy-policies": "set-policies",
  "privacy-reviews": "reviews",
  "privacy-memberships": "memberships",
  "privacy-register": "set-register",
  "privacy-case": "cases",
  "case": "cases",
  "privacy-forms": "set-forms",
  "privacy-form": "set-forms",
  "privacy-roles": "set-duties",
  "repos": "repos",
  "admin-repos": "repos",
  "admin-runtimes": "set-runtimes",
  "secrets": "secrets",
  "docs": "docs",
  "admin-pools": "set-pools",
  "admin-workspace-templates": "set-template",
  "admin-network": "set-network",
  "admin-query-log": "set-querylog",
  "git-credentials": "git-creds",
  "packages": "packages",
  "package": "packages",
  "package-imports": "imports",
  "admin-audit": "set-auditlog",
  "deadlines": "deadlines",
};

export function pageVisible(me: Me | null, page: string): boolean {
  return !!me && (me.acting_admin === true || me.pages?.[page] === true);
}

export function can(me: Me | null, action: string, target: string, layer?: string | null): boolean {
  if (me?.acting_admin) return true;
  if (me?.permission_grants?.some((g) => g.target_id === target &&
      ((g.layer_id === "*" && !!layer) || (g.layer_id ?? null) === (layer || null)) && g.actions.includes(action))) return true;
  // Explicit legacy page assignments remain broad global grants during migration.
  return !layer && me?.settings?.[target] === "manage" && !action.endsWith("ceiling.change");
}
