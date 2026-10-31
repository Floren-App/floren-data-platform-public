import type { View } from "./views/Sidebar";

const BASE = import.meta.env.VITE_FDP_DOCS_URL ?? "https://floren-app.github.io/floren-data-platform-public/";

/** Public guide routes only: never append resource identifiers, tokens or query text. */
export function documentationUrl(view: View): string {
  const routes: Record<View, string> = {
    home: "",
    workspaces: "workspaces/",
    workloads: "deploying/",
    data: "storage-integrations/",
    kafka: "",                      // no public Kafka guide yet: the site start page
    apis: "api-management/",
    packages: "packages/",
    catalog: "catalog/",
    access: "rbac/",
    compute: "compute/",
    privacy: "access-policies/",
    admin: "configuration/",
  };
  return new URL(routes[view], BASE.endsWith("/") ? BASE : `${BASE}/`).href;
}
