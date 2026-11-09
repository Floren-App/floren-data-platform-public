import type { View } from "./views/Sidebar";

const BASE = import.meta.env.VITE_FDP_DOCS_URL ?? "https://floren-app.github.io/floren-data-platform-public/";

/** Public guide routes only: never append resource identifiers, tokens or query text. */
export function documentationUrl(view: View): string {
  const routes: Record<View, string> = {
    home: "",
    workspaces: "workspaces/",
    workloads: "deploying/",
    data: "storage-integrations/",
    kafka: "kafka-operations/",
    apis: "api-management/",
    packages: "packages/",
    catalog: "catalog/",
    access: "rbac/",
    compute: "compute/",
    admin: "configuration/",
  };
  return guideUrl(routes[view]);
}

/** A public guide page by its route, such as "platform-recovery/". */
export function guideUrl(route: string): string {
  return new URL(route, BASE.endsWith("/") ? BASE : `${BASE}/`).href;
}
