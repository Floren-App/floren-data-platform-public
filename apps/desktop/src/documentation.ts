import type { View } from "./views/Sidebar";

const BASE = import.meta.env.VITE_FDP_DOCS_URL ?? "https://floren-app.github.io/floren-data-platform-public/";

/** Public guide routes only: never append resource identifiers, tokens or query text. */
export function documentationUrl(view: View): string {
  const routes: Record<View, string> = {
    home: "",
    workspaces: "workspaces/",
    workloads: "deploying/",
    data: "storage-integrations/",
    apis: "api-management/",
    packages: "packages/",
    catalog: "catalog/",
    access: "rbac/",
    compute: "compute/",
    admin: "configuration/",
  };
  return new URL(routes[view], BASE.endsWith("/") ? BASE : `${BASE}/`).href;
}
