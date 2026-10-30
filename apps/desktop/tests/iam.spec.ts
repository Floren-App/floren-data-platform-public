import { expect, test, Page } from "playwright/test";

type Resource = { id: string; kind: string; name: string; parent_id: string | null; object_id: string | null; config: Record<string, unknown>; actions: string[]; data_access?: boolean };
type Grant = { id: string; resource_id: string; app_group_id: string | null; group: string | null;
  principal_id?: string; principal_name?: string; principal_kind?: string; role: string };
const allActions = ["read", "write", "create", "delete", "alter", "execute", "assign", "identity"];
const resource = (id: string, kind: string, name: string, parent: string | null = null, config = {}, objectId: string | null = null): Resource =>
  ({ id, kind, name, parent_id: parent, object_id: objectId, config, actions: [...allActions] });

async function setup(page: Page, access: "owner" | "reader" | "navigation" = "owner", dataAccess?: (r: Resource) => boolean, tokenSeconds = 3600) {
  const resources = [resource("workloads", "root", "Workloads"), resource("finance-folder", "folder", "Finance", "workloads"),
    resource("report-folder", "folder", "Reports", "finance-folder"),
    resource("source-pg", "source", "finance-dw", null, { source_kind: "postgres" }, "pg"),
    resource("source-lake", "source", "lake", null, { source_kind: "iceberg" }, "iceberg"),
    resource("pg-reporting", "schema", "reporting", "source-pg", { state: "ready", executor: "trino", catalog: "finance" }, "pg"),
    resource("lake-reporting", "schema", "reporting", "source-lake", { state: "ready", executor: "trino", catalog: "lake" }, "iceberg"),
    resource("sales-service", "service", "Sales app", "report-folder", {}, "sales"),
    resource("script-service", "service", "Daily report", "report-folder", {}, "script"),
    resource("etl-service", "service", "Service account: finance-etl", null, { service_account: true, account_name: "finance-etl", principal_id: "etl-identity" }, "etl")];
  for (let i = 0; i < 120; i++) resources.push(resource(`pg-schema-${i}`, "schema", `schema_${String(i).padStart(3, "0")}`, "source-pg", { state: "ready" }, "pg"));
  if (access !== "owner") for (const r of resources) r.actions = access === "navigation" && r.id === "source-pg" ? [] : ["read", "execute"];
  if (dataAccess) for (const r of resources) if (r.kind === "source" || r.kind === "schema") r.data_access = dataAccess(r);
  const groups = [{ id: "analysts", name: "Analysts", description: "Finance analysts", owners: ["admin"], bindings: ["idp-finance"], can_manage: true, can_inspect: true },
    { id: "operators", name: "Operators", description: "Platform operators", owners: ["admin"], bindings: [], can_manage: true, can_inspect: true }];
  const grants: Grant[] = [{ id: "parent-grant", resource_id: "source-pg", app_group_id: "operators", group: "Operators", role: "Owner" },
    { id: "schema-grant", resource_id: "pg-reporting", app_group_id: "analysts", group: "Analysts", role: "Reader" }];
  const workloads = [{ id: "sales", resource_id: "sales-service", folder_id: "report-folder", service_principal_id: "sales-identity", execution_mode: "caller",
    actions: access === "owner" ? allActions : ["read", "execute"], slug: "sales", title: "Sales app", kind: "app", runtime: "python", program: "sales", repo: "analytics", branch: "main", owner: "admin",
    allowed_groups: [], warehouse: "small", auto_update: false, status: "live", deployed_sha: "abc1234", deployed_commit: {}, deployed_at: null,
    latest_sha: "abc1234", last_checked_at: null, sync_error: null, program_def: { args: [], kind: "app", runtime: "python", data_sources: [] },
    runtime_version: "3.12", run_as: null, identity: "service:sales", env: {}, secrets: {}, url: "/p/sales/", can_manage: access === "owner", events: [], created_at: "2026-10-17T12:00:00Z" }];
  workloads.push({ ...workloads[0], id: "script", resource_id: "script-service", kind: "script", title: "Daily report", slug: "daily-report", execution_state: "on_demand" } as typeof workloads[0]);
  const catalogItems = [
    { id: "cat-sales", name: "Sales app", kind: "app", resource: resources.find((r) => r.id === "sales-service")!, path: "Workloads / Finance / Reports / Sales app", attributes: { repo: "analytics", branch: "main", runtime: "python" } },
    { id: "cat-script", name: "Daily report", kind: "script", resource: resources.find((r) => r.id === "script-service")!, path: "Workloads / Finance / Reports / Daily report", attributes: { repo: "analytics", branch: "main", runtime: "python" } },
    { id: "cat-table", name: "Revenue", kind: "table", resource: resources.find((r) => r.id === "pg-reporting")!, path: "finance-dw / reporting / revenue", attributes: { object_path: "revenue", columns: [{ name: "amount", data_type: "decimal" }] } },
  ].map((a) => ({ ...a, governance: {} as Record<string, unknown>, sla: {} as Record<string, unknown>, service_level: { status: "not_configured", checks: [] as unknown[] }, can_edit: access === "owner", can_govern: access === "owner", observed_at: "2026-10-18T12:00:00Z", imports: [] }));
  const mutations: { method: string; path: string; body: any }[] = [];
  const reads: string[] = [];
  const dataReads: string[] = [];
  const errors: string[] = [];
  let activeGroup = "analysts";
  let rejectAssignment = false;
  const policies: Record<string, any>[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript((seconds) => {
    localStorage.setItem("fdp.server", "https://iam.test");
    sessionStorage.setItem("oidc.user:https://idp.test:desktop", JSON.stringify({ access_token: "test-token", token_type: "Bearer", expires_at: Math.floor(Date.now() / 1000) + seconds,
      profile: { sub: "admin", preferred_username: "admin" } }));
  }, tokenSeconds);
  await page.route("https://iam.test/api/**", async (route) => {
    const req = route.request(), path = new URL(req.url()).pathname.replace("/api", ""), method = req.method();
    const body = req.postDataJSON();
    const respond = (data: unknown, status = 200) => route.fulfill({ status, json: data, headers: { "Access-Control-Allow-Origin": "*" } });
    if (method !== "GET") mutations.push({ method, path, body }); else reads.push(path);
    if (path === "/v1/layers") return respond([{ id: "1a0000000001", name: "Prod", description: "", workspaces: false, production: true }, { id: "1a0000000002", name: "Non-Prod", description: "", workspaces: true, default: true }]);
    if (path === "/v1/client-config") return respond({ oidc_issuer: "https://idp.test", oidc_client_id: "desktop", oidc_scopes: "openid", public_url: "https://iam.test" });
    if (path === "/v1/me") return respond({ username: "admin", is_admin: access === "owner", active_group_id: activeGroup, available_groups: groups.map((g) => ({ id: g.id, name: g.name })), groups: [], pools: [{ id: "small", name: "small", cpu: 2, memory_gb: 4, max_concurrent: 10, auto_suspend_seconds: 600, usage_available: true, active_runs: 1, active_services: 3, active_products: 2, active_executions: 4, in_use: 4, queued: 2 }], data_sources: [] });
    if (path === "/v1/compute/warehouses") return respond([{ name: "small", cpu: 2, memory_gb: 4, gpu: 0, max_concurrent: 10, in_use: 4, queued: 2,
      layers: [{ layer_id: "1a0000000002", layer_name: "Non-Prod", in_use: 3, queued: 2 }, { layer_id: "1a0000000001", layer_name: "Prod", in_use: 1, queued: 0 }],
      occupants: [{ category: "trino", kind: "workers", state: "running", slots: 1, name: "Trino workers", owner: "platform", layer_id: "1a0000000001", layer_name: "Prod", queue_position: null, run_id: null, workload_id: null, workspace_id: null },
        { category: "app", kind: "app", state: "running", slots: 1, name: "Sales app", owner: "admin", layer_id: "1a0000000001", layer_name: "Prod", queue_position: null, run_id: null, workload_id: "sales", workspace_id: null },
        { category: "job", kind: "script", state: "queued", slots: 1, name: "etl.py", owner: "admin", layer_id: "1a0000000002", layer_name: "Non-Prod", queue_position: 2, run_id: "run-9", workload_id: null, workspace_id: null }],
      others_in_use: 3, others_queued: 1, workspaces: { running: 2, queued: 0, max_running: 5 } }]);
    if (path === "/v1/me/active-group" && method === "PUT") { activeGroup = body.group_id; return respond({ active_group_id: activeGroup }); }
    if (path === "/v1/rbac/resources") return respond(resources);
    if (path === "/v1/catalog/suggest") return respond({ items: catalogItems.filter((a) => a.name.toLowerCase().includes(new URL(req.url()).searchParams.get("q")!.toLowerCase())) });
    if (path === "/v1/catalog/search") {
      const params = new URL(req.url()).searchParams;
      const all = catalogItems.filter((a) => (!params.get("q") || JSON.stringify(a).toLowerCase().includes(params.get("q")!.toLowerCase())) && (!params.get("kind") || a.kind === params.get("kind")) && (!params.get("status") || a.governance.status === params.get("status")));
      const offset = Number(params.get("offset") || 0);
      return respond({ items: all.slice(offset, offset + 30), offset, limit: 30, total: all.length, facets: { kind: ["app", "script", "table"], domain: ["Finance"], branch: ["main"] } });
    }
    if(path==='/v1/catalog/alerts'||path.endsWith('/sla-history')||path.endsWith('/metrics'))return respond([]);
    if(path.endsWith('/evaluate-sla'))return respond({status:'unknown',checks:[]});
    const catalogRequest = path.match(/^\/v1\/catalog\/assets\/([^/]+)(?:\/(governance|sla|lineage))?$/);
    if (catalogRequest) {
      const [, id, section] = catalogRequest, item = catalogItems.find((a) => a.id === id)!;
      if (section === "governance" && method === "PUT") item.governance = body;
      if (section === "sla" && method === "PUT") { item.sla = body; item.service_level = { status: "unknown", checks: [{ name: "Freshness", target: body.freshness_minutes, value: null, unit: "minutes", status: "unknown" }] }; }
      if (section === "lineage") return respond({ nodes: catalogItems.map((a, index) => ({ ...a, distance: a.id === id ? 0 : index === 2 ? -1 : 1 })),
        edges: [{ id: "native-edge", source: "cat-table", target: "cat-script", relation: "reads", origin: "dbt_manifest", revision: "abc1234", observed_at: "2026-10-18T12:00:00Z" },
          { id: "program-edge", source: "cat-script", target: "cat-sales", relation: "orchestrates", origin: "airflow_dag", revision: "abc1234", observed_at: "2026-10-18T12:00:00Z" }], truncated: false });
      return respond(item);
    }
    if (path.startsWith("/v1/data/schemas/")) {
      dataReads.push(req.url());
      if (path.endsWith("/preview")) return respond({ columns: ["id", "amount"], rows: Array.from({ length: 25 }, (_, i) => [i, i === 0 ? null : i * 10]), limit: 25, query_id: "preview" });
      const params = new URL(req.url()).searchParams;
      return respond({ children: params.has("table") ? [
        { name: "id", kind: "column", data_type: "bigint", nullable: false, ordinal_position: 1 },
        { name: "amount", kind: "column", data_type: "decimal(12,2)", nullable: true, ordinal_position: 2 },
      ] : [{ name: "sales", kind: "table" }, { name: "sales_summary", kind: "view" }], next_cursor: null });
    }
    if (path === "/v1/rbac/groups") return respond(groups);
    if (path === "/v1/rbac/directory") return respond([{ id: "admin", name: "Admin", kind: "user", active: true },
      { id: "ana", name: "ana", kind: "user", active: true, provider: "https://idp.test", external_id: "ana-external", managed: true, provisioning: "scim", origin: "idp", platform_admin: true, member_of: ["idp-finance"], app_groups: ["analysts"] },
      { id: "idp-finance", name: "Finance IdP", kind: "group", active: true, provider: "https://idp.test", managed: true, origin: "idp", members: ["ana"] },
      { id: "etl-identity", name: "finance-etl", kind: "service", active: true, origin: "app" }]);
    const groupGrants = path.match(/^\/v1\/rbac\/groups\/([^/]+)\/assignments$/);
    if (groupGrants) return respond(grants.filter((g) => g.app_group_id === groupGrants[1]).map((g) => ({ resource: resources.find((r) => r.id === g.resource_id), role: g.role })));
    if (path === "/v1/workloads") {
      if (method === "POST") {
        const id = "new-workload", resourceId = "new-service";
        const created = { ...workloads[0], ...body, id, resource_id: resourceId, folder_id: body.folder_id, title: body.title,
          repo: "analytics", program_def: { args: [], kind: "script", runtime: "python", data_sources: [] }, kind: "script", url: null,
          execution_state: "on_demand" } as unknown as typeof workloads[0];
        workloads.push(created); resources.push(resource(resourceId, "service", body.title, body.folder_id, {}, id));
        return respond(created, 201);
      }
      return respond(workloads);
    }
    if (path === "/v1/workloads/new-workload") return respond(workloads.find((p) => p.id === "new-workload"));
    if (path === "/v1/workloads/sales") {
      if (method === "PATCH") Object.assign(workloads[0], body);
      return respond(workloads[0]);
    }
    if (path === "/v1/workloads/script") return respond(workloads[1]);
    if (path === "/v1/rbac/folders" && method === "POST") {
      const created = resource(`created-folder-${resources.length}`, "folder", body.name, body.parent_id);
      resources.push(created); return respond(created, 201);
    }
    if (path.endsWith('/service-principals')) return respond([{ id: 'etl-identity', name: 'finance-etl' },
      { id: 'sales-identity', name: 'Sales app' }, { id: 'script-identity', name: 'Daily report' }]);
    const serviceRevoke = path.match(/^\/v1\/rbac\/resources\/([^/]+)\/assignments\/services\/([^/]+)$/);
    if (serviceRevoke && method === 'DELETE') {
      const index = grants.findIndex((g) => g.resource_id === serviceRevoke[1] && g.principal_id === serviceRevoke[2]);
      if (index >= 0) grants.splice(index, 1);
      return route.fulfill({ status: 204 });
    }
    const assignment = path.match(/^\/v1\/rbac\/resources\/([^/]+)\/assignments(?:\/([^/]+))?$/);
    if (assignment) {
      const [, id, groupId] = assignment;
      if (method === "PUT") {
        if (rejectAssignment) return respond({ detail: "Owner permission required" }, 403);
        const previous = grants.find((g) => g.resource_id === id && (body.principal_id ? g.principal_id === body.principal_id : g.app_group_id === body.app_group_id));
        if (previous) previous.role = body.role;
        else grants.push({ id: `grant-${grants.length}`, resource_id: id, app_group_id: body.app_group_id ?? null,
          group: body.app_group_id ? groups.find((g) => g.id === body.app_group_id)!.name : null,
          principal_id: body.principal_id, principal_name: body.principal_id === 'etl-identity' ? 'finance-etl' : body.principal_id === 'ana' ? 'Ana' : undefined, principal_kind: body.principal_id === 'ana' ? 'user' : 'service', role: body.role });
      }
      if (method === "DELETE") {
        const index = grants.findIndex((g) => g.resource_id === id && g.app_group_id === groupId);
        if (index >= 0) grants.splice(index, 1);
        return route.fulfill({ status: 204 });
      }
      const chain = new Set<string>(); let current = resources.find((r) => r.id === id);
      while (current) { chain.add(current.id); current = resources.find((r) => r.id === current!.parent_id); }
      return respond(grants.filter((g) => chain.has(g.resource_id)).map((g) => ({ ...g, inherited: g.resource_id !== id })));
    }
    if (path === "/v1/data-policies" && method === "GET") return respond(policies.filter((p) => p.schema_id === new URL(req.url()).searchParams.get("schema_id")));
    if (path === "/v1/data-policies" && method === "POST") {
      const created = { id: `policy-${policies.length + 1}`, member_groups: [], updated_by: "admin", updated_at: null, ...body, table: body.table.toLowerCase() };
      policies.push(created);
      return respond(created, 201);
    }
    const policyPath = path.match(/^\/v1\/data-policies\/([^/]+)$/);
    if (policyPath && method === "PUT") { const p = policies.find((x) => x.id === policyPath[1])!; Object.assign(p, body); return respond(p); }
    if (policyPath && method === "DELETE") { policies.splice(policies.findIndex((x) => x.id === policyPath[1]), 1); return route.fulfill({ status: 204 }); }
    if (path === "/v1/service-accounts") return respond([{ id: "etl", name: "finance-etl", principal: "sa:finance-etl", can_manage: access === "owner", description: "Finance ETL", groups: [], allowed_groups: [], disabled: false }]);
    if (path === "/v1/secrets") return respond([{ name: "api-key", ref: "me/api-key", scope: "user", owner: "admin", description: "External API", updated_at: "2026-10-17T12:00:00Z", updated_by: "admin" }]);
    if (path === "/v1/repos") return respond([{ id: "analytics", name: "analytics", url: "https://git.test/analytics" }]);
    if (path === "/v1/repos/analytics/branches") return respond([{ name: "main", sha: "abc1234" }]);
    if (path === "/v1/repos/analytics/pull") return respond({ commit: { sha: "abc1234" }, errors: [], programs: { transform: { kind: "script", runtime: "python", entrypoint: "transform.py", data_sources: [], args: [], warehouse: "small" } } });
    if (path === "/v1/workspaces" || path === "/v1/runs" || path.endsWith("/tokens")) return respond([]);
    return respond({ detail: `Unexpected test request: ${method} ${path}` }, 404);
  });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Storage", exact: true })).toBeVisible();
  return { mutations, reads, dataReads, errors, catalogItems, resources, workloads, activeGroup: () => activeGroup, rejectAssignment: () => { rejectAssignment = true; } };
}

async function openSchema(page: Page, source = "finance-dw") {
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: source, exact: true }).click();
  await page.locator(".main").getByRole("button", { name: "reporting", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Permissions", exact: true }).click();
  await expect(page.getByRole("region", { name: "Permissions for reporting" })).toBeVisible();
}

test("Kafka opens topics lazily and keeps data roles on the topic permission page", async ({ page }) => {
  const state = await setup(page);
  const cluster = resource("kafka-cluster", "kafka", "Event streaming", null, { layer_id: "1a0000000002", broker_count: 1 });
  const topic = resource("card-topic", "topic", "pokemon.cards", cluster.id, { layer_id: "1a0000000002", phase: "Ready", partitions: 3, replication_factor: 1, retention_ms: 604800000, cleanup_policy: "delete" });
  topic.actions.push("ddl"); cluster.actions.push("ddl");
  state.resources.push(cluster);
  let topicLists = 0;
  await page.route("https://iam.test/api/v1/kafka/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/clusters/kafka-cluster")) return route.fulfill({ json: cluster });
    if (path.endsWith("/topics/card-topic")) return route.fulfill({ json: topic });
    if (path.endsWith("/topics")) { topicLists++; return route.fulfill({ json: [topic] }); }
    return route.fulfill({ status: 404, json: {} });
  });
  await page.reload();
  // Kafka has its own activity; Storage no longer lists it.
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await expect(page.locator(".sidebar").getByRole("button", { name: "Event streaming", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Kafka", exact: true }).click();
  await expect(page.locator(".main").getByRole("button", { name: "Event streaming", exact: true })).toBeVisible();
  const sidebar = page.locator(".sidebar");
  await expect(sidebar.getByRole("button", { name: "Event streaming", exact: true })).toBeVisible();
  expect(topicLists).toBe(0);
  // Expanding the cluster in the tree loads its topics, aligned like other trees.
  await sidebar.getByRole("button", { name: "Expand Kafka cluster Event streaming" }).click();
  await expect(sidebar.getByRole("list", { name: "Topics in Event streaming" }).getByRole("button", { name: "pokemon.cards" })).toBeVisible();
  expect(topicLists).toBeGreaterThan(0); // React StrictMode may repeat effects in the dev server.
  await sidebar.getByRole("button", { name: "Event streaming", exact: true }).click();
  await expect(page.locator(".main").getByRole("button", { name: "pokemon.cards", exact: true })).toBeVisible();
  await page.locator(".main").getByRole("button", { name: "pokemon.cards", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Permissions", exact: true }).click();
  const panel = page.getByRole("region", { name: "Permissions for pokemon.cards" });
  await expect(panel).toBeVisible();
  await expect(panel.getByLabel("Role", { exact: true })).toHaveValue("Reader");
  await expect(panel.getByRole("option", { name: /Executor/ })).toHaveCount(0);
  await expect(panel).toContainText("Consumer groups are isolated by identity");
});

test("platform metadata has a SQL tab with results and validated mutation errors", async ({ page }) => {
  const state = await setup(page);
  const schema = state.resources.find((r) => r.id === "pg-reporting")!;
  schema.config = { ...schema.config, system_metadata: true, catalog: "fdp_metadata" };
  await page.reload();
  let submitted = "";
  await page.route("https://iam.test/api/v1/sql/query", async (route) => {
    submitted = route.request().postDataJSON().sql;
    if (submitted.startsWith("DELETE")) return route.fulfill({ status: 422, json: { detail: "This metadata table is read-only" } });
    return route.fulfill({ json: { columns: ["name"], rows: [["Global Admin"]], rowcount: 1, truncated: false, query_id: "sql-1" } });
  });
  await openSchema(page);
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "SQL", exact: true }).click();
  await expect(page.getByLabel("SQL statement")).toHaveValue(/fdp_metadata/);
  await page.getByRole("button", { name: "Run SQL", exact: true }).click();
  await expect(page.locator(".main").getByRole("cell", { name: "Global Admin", exact: true })).toBeVisible();
  expect(submitted).toContain("app_groups");
  await page.getByLabel("SQL statement").fill("DELETE FROM fdp_metadata.public.audit_events WHERE id='x'");
  await page.getByRole("button", { name: "Run SQL", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("read-only");
  expect(state.errors).toEqual([]);
});

test("schema IAM stays scoped among 120 schemas and identical cross-source names", async ({ page }) => {
  const state = await setup(page);
  await openSchema(page);
  await expect(page.getByRole("button", { name: "Remove Analysts's direct Reader role" })).toBeVisible();
  await expect(page.getByRole("button", { name: /Remove Operators/ })).toHaveCount(0);
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await page.getByLabel("Role", { exact: true }).selectOption("Contributor");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Assigned Contributor to Analysts.");
  expect(state.mutations).toContainEqual({ method: "PUT", path: "/v1/rbac/resources/pg-reporting/assignments", body: { app_group_id: "analysts", role: "Contributor" } });
  await openSchema(page, "lake");
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Assigned Reader to Analysts.");
  expect(state.mutations.at(-1)?.path).toBe("/v1/rbac/resources/lake-reporting/assignments");
  expect(state.errors).toEqual([]);
});

test("schema Owners grant and revoke Contributor directly to a service without creating a group", async ({ page }) => {
  const state = await setup(page);
  await openSchema(page);
  const panel = page.getByRole('region', { name: 'Permissions for reporting', exact: true });
  await panel.getByRole('combobox', { name: 'Grant to', exact: true }).selectOption('service');
  await panel.getByLabel('Find a service identity', { exact: true }).fill('finance');
  await panel.getByRole('combobox', { name: 'Service identity', exact: true }).selectOption('etl-identity');
  await panel.getByLabel('Role', { exact: true }).selectOption('Contributor');
  await panel.getByRole('button', { name: 'Assign role', exact: true }).click();
  await expect(panel.getByRole('status')).toHaveText('Assigned Contributor to finance-etl.');
  const row = panel.getByRole('row').filter({ hasText: 'finance-etl' });
  await expect(row).toContainText('Service · direct');
  await expect(row).toContainText('Contributor');
  const grant = state.mutations.find((m) => m.method === 'PUT');
  expect(grant?.body).toEqual({ principal_id: 'etl-identity', role: 'Contributor' });
  expect(grant?.path).toBe('/v1/rbac/resources/pg-reporting/assignments');
  expect(state.mutations.some((m) => m.path === '/v1/rbac/groups')).toBe(false);
  await row.getByRole('button', { name: "Remove finance-etl's direct Contributor role" }).click();
  await expect(row).toHaveCount(0);
  expect(state.mutations.at(-1)?.path).toBe('/v1/rbac/resources/pg-reporting/assignments/services/etl-identity');
  expect(state.errors).toEqual([]);
});

test("inherited grant opens the parent's permissions and direct revoke targets the child", async ({ page }) => {
  const state = await setup(page);
  await openSchema(page);
  await page.getByRole("button", { name: "Remove Analysts's direct Reader role" }).click();
  await expect(page.getByRole("status")).toHaveText("Removed the direct role for Analysts.");
  expect(state.mutations.at(-1)?.path).toBe("/v1/rbac/resources/pg-reporting/assignments/analysts");
  await page.getByRole("cell", { name: "finance-dw", exact: true }).getByRole("button").click();
  await expect(page.getByRole("region", { name: "Permissions for finance-dw" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Remove Operators's direct Owner role" })).toBeVisible();
});

test("readers see grants without permission editing or service identity controls", async ({ page }) => {
  await setup(page, "reader");
  await openSchema(page);
  await expect(page.getByText("Inherited", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Assign role", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^Remove/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await page.getByRole("button", { name: /Finance$/, exact: false }).last().click();
  await page.getByRole("button", { name: /Reports$/ }).last().click();
  await page.locator(".main").getByRole("button", { name: "Sales app", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Settings", exact: true })).toHaveCount(0);
});

test("navigation-only ancestors do not fetch protected permission details", async ({ page }) => {
  const state = await setup(page, "navigation");
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "finance-dw", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Permissions", exact: true }).click();
  await expect(page.getByText(/Permission details require access/)).toBeVisible();
  expect(state.reads).not.toContain("/v1/rbac/resources/source-pg/assignments");
});

test("nested folder creation and workload grants use the current object", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await page.getByRole("button", { name: /Finance$/ }).last().click();
  await page.getByRole("button", { name: /Reports$/ }).last().click();
  await page.locator(".main").getByRole("button", { name: "Sales app", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Permissions", exact: true }).click();
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await expect(page.getByLabel("Role", { exact: true })).toHaveValue("Executor");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Assigned Executor to Analysts.");
  expect(state.mutations.at(-1)?.path).toBe("/v1/rbac/resources/sales-service/assignments");
  await page.getByRole("navigation", { name: "Object location" }).getByRole("button", { name: "Reports", exact: true }).click();
  await page.locator(".main").getByRole("button", { name: "New folder", exact: true }).click();
  await page.getByLabel("Folder name", { exact: true }).fill("Monthly");
  await page.getByRole("button", { name: "Create folder", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Monthly", exact: true })).toBeVisible();
  expect(state.mutations.at(-1)).toEqual({ method: "POST", path: "/v1/rbac/folders", body: { name: "Monthly", parent_id: "report-folder" } });
  await page.screenshot({ path: test.info().outputPath("nested-folder.png") });
});

test("deploy here keeps the folder and the workload wizard shows full folder paths", async ({ page }) => {
  await setup(page);
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await page.getByRole("button", { name: /Finance$/ }).last().click();
  await page.getByRole("button", { name: /Reports$/ }).last().click();
  await page.getByRole("button", { name: "Deploy here", exact: true }).click();
  await page.getByRole("button", { name: "Pull", exact: true }).click();
  await expect(page.getByLabel("Workload folder", { exact: true })).toHaveValue("report-folder");
  await expect(page.getByLabel("Workload folder", { exact: true }).getByRole("option", { name: "Workloads / Finance / Reports", exact: true })).toHaveCount(1);
});

test("the + Deploy action completes workload creation using caller permissions", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await page.getByRole("button", { name: "Add workload", exact: true }).click();
  await page.getByRole("menuitem", { name: "Deploy", exact: true }).click();
  await page.getByRole("button", { name: "Pull", exact: true }).click();
  await expect(page.getByLabel("Data identity", { exact: true })).toHaveValue("caller");
  await page.getByLabel("Title", { exact: true }).fill("New transform");
  await page.getByLabel("Workload folder", { exact: true }).selectOption("report-folder");
  await page.getByRole("button", { name: "Deploy transform", exact: true }).click();
  await expect(page.getByRole("heading", { name: "New transform", exact: true })).toBeVisible();
  const request = state.mutations.find((m) => m.method === "POST" && m.path === "/v1/workloads");
  expect(request?.body).toMatchObject({ program: "transform", folder_id: "report-folder", execution_mode: "caller", slug: "transform", branch: "main" });
  expect(state.errors).toEqual([]);
});

test("Deployed exposes folder creation and opens the new folder's IAM", async ({ page }) => {
  const state = await setup(page);
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "New folder", exact: true })).toHaveCount(0);
  await sidebar.getByRole("button", { name: "Add workload", exact: true }).click();
  await page.getByRole("menuitem", { name: "Deploy", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Deploy", exact: true })).toBeVisible();
  await expect(page.getByRole("menu", { name: "Create in Workloads" })).toHaveCount(0);
  await sidebar.getByRole("button", { name: "Add workload", exact: true }).click();
  await page.getByRole("menuitem", { name: "Folder", exact: true }).click();
  await main.getByLabel("Folder name", { exact: true }).fill("Research");
  await main.getByRole("button", { name: "Create folder", exact: true }).click();
  await expect(page.getByRole("region", { name: "Permissions for Research" })).toBeVisible();
  expect(state.mutations.at(-1)).toEqual({ method: "POST", path: "/v1/rbac/folders", body: { name: "Research", parent_id: "workloads" } });
  // The shortcut also works when its destination tab is already open.
  await sidebar.getByRole("button", { name: "New folder in Finance", exact: true }).click();
  await expect(main.getByRole("heading", { name: "New folder in Finance", exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Cancel", exact: true }).click();
  await sidebar.getByRole("button", { name: "New folder in Finance", exact: true }).click();
  await main.getByLabel("Folder name", { exact: true }).fill("prd");
  await main.getByRole("button", { name: "Create folder", exact: true }).click();
  await expect(page.getByRole("region", { name: "Permissions for prd" })).toBeVisible();
  expect(state.mutations.at(-1)?.body).toEqual({ name: "prd", parent_id: "finance-folder" });
  expect(state.errors).toEqual([]);
});

test("folder IAM flows to apps and nested folders while keeping branches visible", async ({ page }) => {
  const state = await setup(page);
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await sidebar.getByRole("button", { name: "Permissions for folder Finance", exact: true }).click();
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await page.getByLabel("Role", { exact: true }).selectOption("Executor");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Assigned Executor to Analysts.");
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Contents", exact: true }).click();
  await main.getByRole("button", { name: "Reports", exact: true }).click();
  await expect(main.getByRole("columnheader", { name: "Git branch", exact: true })).toBeVisible();
  await expect(main.getByRole("row").filter({ hasText: "Sales app" }).getByText("main", { exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Sales app", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Permissions", exact: true }).click();
  const inherited = main.getByRole("row").filter({ hasText: "Analysts" });
  await expect(inherited.getByText("Inherited", { exact: true })).toBeVisible();
  await expect(inherited.getByRole("button", { name: /Remove/ })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("deployed folder readers have no folder creation shortcuts", async ({ page }) => {
  await setup(page, "reader");
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await expect(page.getByRole("button", { name: /^New folder/ })).toHaveCount(0);
  await page.locator(".sidebar").getByRole("button", { name: "Permissions for folder Finance", exact: true }).click();
  await expect(page.getByRole("region", { name: "Permissions for Finance" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Assign role", exact: true })).toHaveCount(0);
});

test("service permissions and identity bindings have their own contextual pages", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "Security", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Services", exact: true }).click();
  await page.getByRole("button", { name: "Service account: finance-etl", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Permissions", exact: true }).click();
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await page.getByLabel("Role", { exact: true }).selectOption("Owner");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Assigned Owner to Analysts.");
  expect(state.mutations.at(-1)?.path).toBe("/v1/rbac/resources/etl-service/assignments");
  await page.getByRole("cell", { name: "Analysts", exact: true }).getByRole("button").click();
  await expect(page.getByRole("heading", { name: "Identity bindings", exact: true })).toBeVisible();
  await expect(page.getByRole("table", { name: "Identity bindings", exact: true }).getByRole("cell", { name: /^Finance IdP/ })).toBeVisible();
  await expect(page.getByLabel("Folder, service, or schema")).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("group-bindings.png") });
  expect(state.errors).toEqual([]);
});

const deniedData = {
  code: "data_access_denied", message: 'Service account "finance-etl" has no data access to "finance-dw".',
  guidance: "Assign a data role on the required schema and bind this service account to the app security group.",
  identity: { name: 'Service account "finance-etl"', principal_id: "etl-identity", kind: "service" }, can_manage_groups: true,
  sources: [{ name: "finance-dw", resource_id: "source-pg", can_assign: true }],
};

test("data access failures name the identity and link to permissions and preselected group bindings", async ({ page }) => {
  const state = await setup(page);
  Object.assign(state.workloads[0], { status: "failed", execution_mode: "fixed", run_as: "finance-etl", sync_error: deniedData.message, access_issue: deniedData });
  await page.reload();
  await page.getByRole("button", { name: "Security", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Services", exact: true }).click();
  const main = page.locator(".main");
  await main.getByRole("button", { name: "Sales app", exact: true }).click();
  const alert = main.getByRole("alert");
  await expect(alert.getByRole("heading", { name: "Data access required" })).toBeVisible();
  await expect(alert).toContainText(deniedData.message);
  await main.getByRole("button", { name: "Pin Sales app", exact: true }).click();
  await alert.getByRole("button", { name: "finance-dw permissions", exact: true }).click();
  await expect(main.getByRole("region", { name: "Permissions for finance-dw", exact: true })).toBeVisible();
  await expect(main.getByRole("combobox", { name: "Grant to", exact: true })).toHaveValue("service");
  await expect(main.getByRole("combobox", { name: "Service identity", exact: true })).toHaveValue("etl-identity");
  await page.getByRole("tab", { name: /Sales app/ }).click();
  await alert.getByRole("button", { name: "Manage identity bindings", exact: true }).click();
  await main.getByRole("button", { name: /^Analysts/ }).click();
  await expect(main.getByRole("combobox", { name: "Identity to bind" })).toHaveValue("etl-identity");
  expect(state.mutations).toEqual([]); // Navigation never silently grants access.
  expect(state.errors).toEqual([]);
});

test("blocked deployed runs render structured permission guidance", async ({ page }) => {
  const state = await setup(page);
  await page.route("https://iam.test/api/v1/workloads/script/runs", (route) => route.fulfill({ status: 403, json: { detail: deniedData } }));
  await page.getByRole("button", { name: "Security", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Services", exact: true }).click();
  const main = page.locator(".main");
  await main.getByRole("button", { name: "Daily report", exact: true }).click();
  await main.getByRole("button", { name: "Run Daily report", exact: true }).click();
  await expect(main.getByRole("alert")).toContainText(deniedData.message);
  await main.getByRole("alert").getByRole("button", { name: "finance-dw permissions", exact: true }).click();
  await expect(main.getByRole("region", { name: "Permissions for finance-dw", exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("retired workload IAM records do not become broken Deployed or Services links", async ({ page }) => {
  const state = await setup(page);
  state.resources.push(resource('retired-api', 'service', 'API verification program', 'workloads', {}, 'retired-api'));
  state.resources.push(resource('retired-smoke', 'service', 'Catalog deploy smoke', 'workloads', {}, 'retired-smk'));
  await page.reload();
  await page.getByRole('button', { name: 'Workloads', exact: true }).click();
  await page.locator('.sidebar').getByRole('button', { name: 'All workloads', exact: true }).click();
  const main = page.locator('.main');
  await expect(main.getByRole('heading', { name: 'Workloads', exact: true })).toBeVisible();
  await expect(main.getByText('API verification program', { exact: true })).toHaveCount(0);
  await expect(main.getByText('Catalog deploy smoke', { exact: true })).toHaveCount(0);
  await expect(main.getByRole('button', { name: 'Finance', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Security', exact: true }).click();
  await page.locator('.sidebar').getByRole('button', { name: 'Services', exact: true }).click();
  await expect(main.getByRole('heading', { name: 'Services', exact: true })).toBeVisible();
  await expect(main.getByText('API verification program', { exact: true })).toHaveCount(0);
  await expect(main.getByText('Catalog deploy smoke', { exact: true })).toHaveCount(0);
  await expect(main.getByRole('button', { name: 'Service account: finance-etl', exact: true })).toBeVisible();
  await main.getByRole('button', { name: 'Sales app', exact: true }).click();
  await expect(main.getByRole('heading', { name: 'Sales app', exact: true })).toBeVisible();
  expect(state.reads).not.toContain('/v1/workloads/retired-api');
  expect(state.reads).not.toContain('/v1/workloads/retired-smk');
  expect(state.errors).toEqual([]);
});

test("permission failures keep the object in view and show the server denial", async ({ page }) => {
  const state = await setup(page);
  await openSchema(page);
  state.rejectAssignment();
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Owner permission required");
  await expect(page.getByRole("region", { name: "Permissions for reporting" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Assign role", exact: true })).toBeEnabled();
});

test("permissions and group bindings fit a compact desktop in dark mode", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 900 });
  await page.emulateMedia({ colorScheme: "dark" });
  await setup(page);
  await openSchema(page);
  await expect(page.getByRole("button", { name: "Assign role", exact: true })).toBeVisible();
  expect(await page.locator(".main").evaluate((main) => main.scrollWidth <= main.clientWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("schema-permissions-compact-dark.png") });
  await page.getByRole("cell", { name: "Analysts", exact: true }).getByRole("button").click();
  await expect(page.getByRole("heading", { name: "Identity bindings", exact: true })).toBeVisible();
  expect(await page.locator(".main").evaluate((main) => main.scrollWidth <= main.clientWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("group-bindings-compact-dark.png") });
});

test("workload identity settings preserve the caller and fixed-service choices", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole("button", { name: "Security", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Services", exact: true }).click();
  await page.locator(".main").getByRole("button", { name: "Sales app", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Settings", exact: true }).click();
  await expect(page.getByLabel("Data identity", { exact: true })).toHaveValue("caller");
  await expect(page.getByLabel("Run as", { exact: true })).toBeDisabled();
  await page.getByLabel("Data identity", { exact: true }).selectOption("fixed");
  await page.getByLabel("Run as", { exact: true }).selectOption("finance-etl");
  await page.getByRole("button", { name: "Save and redeploy", exact: true }).click();
  await expect(page.getByRole("status")).toHaveText("Production settings saved.");
  expect(state.mutations.at(-1)).toEqual({ method: "PATCH", path: "/v1/workloads/sales", body: { execution_mode: "fixed", run_as: "finance-etl", env: {}, secrets: {} } });
  expect(state.errors).toEqual([]);
});

test("data children are fetched only when opened and shared between tree and detail pages", async ({ page }) => {
  const state = await setup(page);
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Storage", exact: true })).toBeVisible();
  expect(state.dataReads).toEqual([]);
  await sidebar.getByRole("button", { name: "finance-dw", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "Expand schema reporting", exact: true })).toBeVisible();
  expect(state.dataReads).toEqual([]);
  await sidebar.getByRole("button", { name: "Expand schema reporting", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "sales", exact: true })).toBeVisible();
  expect(state.dataReads.length).toBe(1);
  await main.getByRole("button", { name: "reporting", exact: true }).click();
  await expect(main.getByRole("button", { name: "sales", exact: true })).toBeVisible();
  expect(state.dataReads.length).toBe(1);
  await sidebar.getByRole("button", { name: "Expand table sales", exact: true }).click();
  await expect(sidebar.getByText("amount", { exact: true })).toBeVisible();
  expect(state.dataReads.length).toBe(2);
  expect(new URL(state.dataReads[1]).searchParams.get("table")).toBe("sales");
  await main.getByRole("button", { name: "sales", exact: true }).click();
  await expect(main.getByRole("cell", { name: "decimal(12,2)", exact: true })).toBeVisible();
  expect(state.dataReads.length).toBe(2);
  await sidebar.getByRole("button", { name: "Collapse table sales", exact: true }).click();
  await sidebar.getByRole("button", { name: "Expand table sales", exact: true }).click();
  await expect(sidebar.getByText("amount", { exact: true })).toBeVisible();
  expect(state.dataReads.length).toBe(2);
  await page.screenshot({ path: test.info().outputPath("data-hierarchy.png") });
  await main.getByRole("button", { name: "Refresh columns", exact: true }).click();
  await expect.poll(() => state.dataReads.length).toBe(3);
  expect(state.errors).toEqual([]);
});

test("runtime polling does not query data objects or reload the resource hierarchy", async ({ page }) => {
  await page.clock.install();
  const state = await setup(page);
  await openSchema(page);
  const metadataCount = state.dataReads.length;
  const registryCount = state.reads.filter((path) => path === "/v1/rbac/resources").length;
  const statusCount = state.reads.filter((path) => path === "/v1/workloads").length;
  await page.clock.fastForward(15000);
  await expect.poll(() => state.reads.filter((path) => path === "/v1/workloads").length).toBeGreaterThan(statusCount);
  expect(state.dataReads.length).toBe(metadataCount);
  expect(state.reads.filter((path) => path === "/v1/rbac/resources").length).toBe(registryCount);
});

test("security sidebar links its four pages and secrets live there", async ({ page }) => {
  const state = await setup(page);
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("button", { name: "Security", exact: true }).click();
  await expect(main.getByRole("heading", { name: "App groups", exact: true })).toBeVisible();
  // Lists and detail belong to the pages; the sidebar only switches between them.
  await expect(sidebar.getByRole("button")).toHaveText(["", "App groups", "Users", "Services", "Secrets"]);
  await expect(page.getByRole("button", { name: "Secrets", exact: true }).and(page.locator(".activity button"))).toHaveCount(0);
  await main.getByRole("button", { name: /^Analysts/ }).click();
  await expect(main.getByRole("heading", { name: "Analysts", exact: true })).toBeVisible();
  await sidebar.getByRole("button", { name: "Secrets", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "Secrets", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(main.getByRole("heading", { name: "Secrets", exact: true })).toBeVisible();
  await main.getByRole("button", { name: "me/api-key", exact: true }).click();
  const detail = main.getByRole("region", { name: "Secret api-key" });
  await expect(detail.getByText("String value (never shown)")).toBeVisible();
  await expect(detail.getByText('"secrets": { "API_KEY": "me/api-key" }')).toBeVisible();
  await detail.getByRole("button", { name: "Replace", exact: true }).click();
  await expect(main.getByLabel("Name", { exact: true })).toHaveValue("api-key");
  expect(state.reads.filter((path) => path === "/v1/secrets").length).toBe(1);
  expect(state.dataReads).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("IAM has separate group user and service pages with explicit identity origins", async ({ page }) => {
  const state = await setup(page);
  const main = page.locator('.main');
  await page.getByRole('button', { name: 'Security', exact: true }).click();
  const navigation = page.getByRole('navigation', { name: 'Security pages' });
  await expect(navigation.getByRole('button')).toHaveCount(4);
  await expect(main.getByRole('heading', { name: 'IdP groups & roles', exact: true })).toBeVisible();
  await navigation.getByRole('button', { name: 'Users', exact: true }).click();
  await expect(main.getByRole('heading', { name: 'Users', exact: true })).toBeVisible();
  await main.getByRole('button', { name: 'ana', exact: true }).click();
  await expect(main.getByRole('cell', { name: 'IdP · SCIM', exact: true })).toBeVisible();
  await expect(main.getByRole('cell', { name: 'Platform admin', exact: true })).toBeVisible();
  await expect(main.getByRole('heading', { name: 'App security groups', exact: true })).toBeVisible();
  await navigation.getByRole('button', { name: 'Services', exact: true }).click();
  await expect(main.getByRole('columnheader', { name: 'Execution identity', exact: true })).toBeVisible();
  await expect(main.getByRole('cell', { name: 'Caller for data requests', exact: true }).first()).toBeVisible();
  await expect(main.getByRole('cell', { name: 'External / shared identity', exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
  await page.screenshot({ path: test.info().outputPath('iam-services.png') });
});

test("preview queries only when opened and displays at most 25 rows", async ({ page }) => {
  const state = await setup(page);
  await openSchema(page);
  const main = page.locator('.main');
  await page.getByRole('navigation', { name: 'Object sections' }).getByRole('button', { name: 'Overview', exact: true }).click();
  await main.getByRole('button', { name: 'sales', exact: true }).click();
  await expect(main.getByRole('heading', { name: 'Columns', exact: true })).toBeVisible();
  expect(state.dataReads.some((url) => url.includes('/preview'))).toBe(false);
  await page.getByRole('navigation', { name: 'Object sections' }).getByRole('button', { name: 'Data preview', exact: true }).click();
  const region = page.getByRole('region', { name: 'Data preview of sales', exact: true });
  await expect(region.locator('tbody tr')).toHaveCount(25);
  await expect(region.getByRole('cell', { name: 'NULL', exact: true })).toBeVisible();
  expect(state.dataReads.filter((url) => url.includes('/preview')).length).toBe(1);
  await page.screenshot({ path: test.info().outputPath('table-preview.png') });
  expect(state.errors).toEqual([]);
});

test("compute counts active products and scripts are ready on demand", async ({ page }) => {
  const state = await setup(page);
  await page.getByRole('button', { name: 'Compute pools', exact: true }).click();
  await expect(page.locator('.sidebar').getByText('4/10 +2', { exact: true })).toBeVisible();
  await page.locator('.sidebar').getByRole('button', { name: /small/ }).click();
  const main = page.locator('.main');
  await expect(main.getByRole('meter', { name: 'Slots in use' })).toHaveAttribute('aria-valuenow', '4');
  await expect(main.getByRole('table', { name: 'Use by layer' })).toContainText('Non-Prod32');
  const using = main.getByRole('table', { name: 'Using this warehouse' });
  await expect(using.getByRole('row', { name: /Sales app/ })).toContainText('App');
  await expect(using.getByRole('row', { name: /Trino workers/ })).toContainText('SQL engine');
  await expect(using.getByRole('row', { name: /etl\.py/ })).toContainText('queued · 2nd in line');
  await expect(main.getByText('Work of others you cannot see: 3 slots in use, 1 waiting.', { exact: true })).toBeVisible();
  await expect(main.getByText(/2 running of 5 \(separate limit\)/)).toBeVisible();
  await using.getByRole('button', { name: 'Sales app', exact: true }).click();
  await expect(main.getByRole('heading', { name: 'Sales app' })).toBeVisible();
  await page.getByRole('button', { name: 'Compute pools', exact: true }).click();
  await page.locator('.sidebar').getByRole('button', { name: /small/ }).click();
  await page.getByRole('button', { name: 'Workloads', exact: true }).click();
  await page.locator('.main').getByRole('button', { name: /Finance$/ }).click();
  await page.locator('.main').getByRole('button', { name: /Reports$/ }).click();
  await page.locator('.main').getByRole('button', { name: 'Daily report', exact: true }).click();
  await expect(page.locator('.main').getByText(/Ready to run · Program/)).toBeVisible();
  await expect(page.locator('.main').getByRole('heading', { name: 'Runs on demand', exact: true })).toBeVisible();
  await page.locator('.main').getByText('Call from an API or Airflow', { exact: true }).click();
  await expect(page.locator('.main').getByText(/workload="daily-report"/)).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("lineage containers reroute all four model and schema expansion combinations", async ({ page }) => {
  const state = await setup(page), project = state.catalogItems[0], base = state.catalogItems[2];
  project.kind = "dbt"; project.name = "Card models";
  const schema = { ...base, id: "schema", name: "reporting", kind: "schema", distance: 1 };
  const nodes = [project, schema,
    { ...base, id: "m1", name: "model_one", kind: "dbt_model", resource: project.resource, lineage_parent_id: project.id },
    { ...base, id: "m2", name: "model_two", kind: "dbt_model", resource: project.resource, lineage_parent_id: project.id },
    { ...base, id: "t1", name: "table_one", lineage_parent_id: schema.id },
    { ...base, id: "t2", name: "table_two", lineage_parent_id: schema.id },
  ].map(node => ({ ...node, distance: 0 }));
  const edges = [["m1", "t1", "writes"], ["m2", "t2", "writes"], ["m1", "m2", "depends_on"]]
    .map(([source,target,relation],i) => ({ id: "detail-"+i, source,target,relation,origin:"dbt_manifest",revision:"sha",observed_at:"2026-10-28T12:00:00Z" }));
  await page.route("https://iam.test/api/v1/catalog/assets/*/lineage?*", route => route.fulfill({ json: { nodes, edges, truncated: false } }));
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  const main = page.locator(".main");
  await main.getByRole("button", { name: "Card models", exact: true }).click();
  await main.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  const graph = main.getByRole("region", { name: "Interactive lineage graph" });
  const summary = graph.locator('[data-id="summary:cat-sales:schema:writes:data:false"]');
  await expect(graph.locator('.react-flow__node')).toHaveCount(2);
  await expect(summary).toBeVisible();
  await summary.locator('.react-flow__edge-textbg').click();
  await expect(main.getByRole("heading", { name: "Underlying connections" })).toBeVisible();
  await expect(main.getByRole("listitem").filter({hasText:"model_one → table_one"})).toBeVisible();
  await main.getByRole("button", { name: "Close lineage details", exact: true }).click();
  await graph.getByRole("button", { name: "Expand contents of Card models", exact: true }).click();
  await expect(graph.locator('[data-id="m1"]')).toBeVisible();
  await expect(graph.locator('[data-id="t1"]')).toHaveCount(0);
  await expect(graph.locator('[data-id="summary:m1:schema:writes:data:false"]')).toBeVisible();
  await graph.getByRole("button", { name: "Collapse contents of Card models", exact: true }).click();
  await graph.getByRole("button", { name: "Expand contents of reporting", exact: true }).click();
  await expect(graph.locator('[data-id="t1"]')).toBeVisible();
  await expect(graph.locator('[data-id="m1"]')).toHaveCount(0);
  await expect(graph.locator('[data-id="summary:cat-sales:t1:writes:data:false"]')).toBeVisible();
  await graph.getByRole("button", { name: "Expand contents of Card models", exact: true }).click();
  await expect(graph.locator('[data-id="m1"]')).toBeVisible();
  await expect(graph.locator('[data-id="t1"]')).toBeVisible();
  await expect(graph.locator('[data-id="detail-0"]')).toBeVisible();
  await expect(graph.locator('[data-id^="summary:"]')).toHaveCount(0);
  await main.getByRole("button", {name:"Fit entire chain",exact:true}).click();
  await expect.poll(async () => {
    const bounds=await graph.boundingBox(), left=await graph.locator('[data-id="cat-sales"]').boundingBox(), right=await graph.locator('[data-id="schema"]').boundingBox();
    return !!bounds && !!left && !!right && left.x >= bounds.x && right.x + right.width <= bounds.x + bounds.width;
  }).toBe(true);
  await page.screenshot({ path: test.info().outputPath("lineage-both-expanded.png"), fullPage:true });
  await graph.getByRole("button", { name: "Collapse contents of reporting", exact: true }).click();
  await expect(graph.locator('[data-id="m1"]')).toBeVisible();
  await expect(graph.locator('[data-id="t1"]')).toHaveCount(0);
  await expect(graph.locator('[data-id="summary:m2:schema:writes:data:false"]')).toBeVisible();
  expect(state.errors).toEqual([]);
});

for (const inspectChild of [false, true]) test(`Airflow hierarchy ${inspectChild ? "reveals ancestors for an inspected task" : "expands program, DAG and tasks independently"}`, async ({ page }) => {
  const state = await setup(page), inspected = state.catalogItems[0], base = state.catalogItems[2];
  inspected.kind = inspectChild ? "airflow_task" : "airflow";
  inspected.name = inspectChild ? "load_cards" : "Pokemon orchestration";
  const program = { ...inspected, id: inspectChild ? "airflow-program" : inspected.id, name: "Pokemon orchestration", kind: "airflow" };
  const dag = { ...base, id: "nested-dag", name: "pokemon_daily", kind: "airflow_dag", lineage_parent_id: program.id };
  const extract = { ...base, id: "extract-task", name: "extract_cards", kind: "airflow_task", lineage_parent_id: dag.id };
  const load = { ...inspected, id: inspectChild ? inspected.id : "load-task", name: "load_cards", kind: "airflow_task", lineage_parent_id: dag.id };
  if (inspectChild) Object.assign(inspected, load);
  const nodes = [program, dag, extract, load].map(node => ({...node, distance: 0}));
  const edges = [{ id: "task-dependency", source: extract.id, target: load.id, relation: "depends_on", origin: "airflow_dag", revision: "sha", observed_at: "2026-10-28T12:00:00Z", control: true }];
  await page.route("https://iam.test/api/v1/catalog/assets/*/lineage?*", route => route.fulfill({json:{nodes,edges,truncated:false}}));
  await page.getByRole("button", {name:"Catalog",exact:true}).click();
  const main=page.locator(".main");
  await main.getByRole("button", {name:inspected.name,exact:true}).click();
  await main.getByRole("navigation", {name:"Catalog sections"}).getByRole("button",{name:"Lineage",exact:true}).click();
  const graph=main.getByRole("region",{name:"Interactive lineage graph"});
  if (!inspectChild) {
    await expect(graph.locator(".react-flow__node")).toHaveCount(1);
    await graph.getByRole("button",{name:"Expand contents of Pokemon orchestration",exact:true}).click();
  }
  await expect(graph.locator(".react-flow__node")).toHaveCount(4);
  await expect(graph.locator(`[data-id="${dag.id}"]`)).toHaveClass(/react-flow__node/);
  await expect(graph.locator(`[data-id="${load.id}"]`)).toBeVisible();
  await expect(graph.locator('[data-id="task-dependency"] .react-flow__edge-path')).toHaveCSS("stroke-dasharray", "5px, 4px");
  await graph.locator(`[data-id="${program.id}"] .lineage-entity-title`).first().hover();
  await expect(graph.locator(`[data-id="${load.id}"]`)).not.toHaveClass(/dimmed/);
  await page.screenshot({path:test.info().outputPath("nested-airflow.png")});
  if (!inspectChild) {
    await graph.getByRole("button",{name:"Collapse contents of pokemon_daily",exact:true}).click();
    await expect(graph.locator(".react-flow__node")).toHaveCount(2);
    await graph.getByRole("button",{name:"Collapse contents of Pokemon orchestration",exact:true}).click();
    await expect(graph.locator(".react-flow__node")).toHaveCount(1);
  }
  expect(state.errors).toEqual([]);
});

test("dbt project lineage groups models and highlights their paths and output impact", async ({ page }) => {
  const state = await setup(page), project = state.catalogItems[0], base = state.catalogItems[2];
  project.kind = "dbt"; project.name = "Card models";
  const nodes = [
    { ...project, distance: 0 },
    { ...base, id: "input", name: "Raw cards", distance: -1 },
    { ...base, id: "staging", name: "stg_cards", kind: "dbt_model", lineage_parent_id: project.id, distance: 0 },
    { ...base, id: "reporting", name: "cards_current", kind: "dbt_model", lineage_parent_id: project.id, distance: 0 },
    { ...base, id: "output", name: "Published cards", distance: 0 },
  ];
  const edges = [["input", "staging", "reads"], ["staging", "reporting", "depends_on"], ["reporting", "output", "writes"]]
    .map(([source, target, relation], i) => ({ id: `edge-${i}`, source, target, relation, origin: "dbt_manifest", revision: "sha", observed_at: "2026-10-19T12:00:00Z" }));
  await page.route("https://iam.test/api/v1/catalog/assets/*/lineage?*", route => route.fulfill({ json: { nodes, edges, truncated: false } }));
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  const main = page.locator(".main");
  await main.getByRole("button", { name: "Card models", exact: true }).click();
  await main.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  const graph = main.getByRole("region", { name: "Interactive lineage graph" });
  await expect(graph.locator('[data-id="cat-sales"]')).toBeVisible();
  await expect(graph.locator('[data-id="staging"]')).toHaveCount(0);
  await graph.getByRole("button", { name: "Expand contents of Card models", exact: true }).click();
  await expect(graph.locator('[data-id="staging"]')).toBeVisible();
  await expect(graph.locator('[data-id="reporting"]')).toBeVisible();
  await graph.locator('[data-id="cat-sales"] .lineage-entity-title').hover();
  await expect(graph.locator('[data-id="staging"]')).not.toHaveClass(/dimmed/);
  await expect(graph.locator('[data-id="output"]')).not.toHaveClass(/dimmed/);
  await page.screenshot({ path: test.info().outputPath("dbt-project-lineage.png") });
  await main.getByRole("button", { name: "Upstream & downstream", exact: true }).click();
  await main.getByRole("button", { name: "Impact analysis", exact: true }).click();
  const impact = main.getByRole("table", { name: "Downstream impact" });
  await expect(impact.getByRole("button", { name: "Published cards", exact: true })).toBeVisible();
  await expect(impact.getByRole("button", { name: "stg_cards", exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("storage and workloads open the same canonical lineage and controls as catalog", async ({ page }) => {
  const state = await setup(page);
  state.catalogItems[2].name = "sales";
  await page.route("https://iam.test/api/v1/catalog/resources/*/asset?*", async route => {
    const url = new URL(route.request().url());
    expect(url.searchParams.get("object_path")).toBe("sales");
    return route.fulfill({ json: state.catalogItems[2] });
  });
  await page.route("https://iam.test/api/v1/catalog/resources/sales-service/asset", route => route.fulfill({ json: state.catalogItems[0] }));
  const main = page.locator(".main"), sidebar = page.locator(".sidebar");
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await sidebar.getByRole("button", { name: "finance-dw", exact: true }).click();
  await main.getByRole("button", { name: "reporting", exact: true }).click();
  await main.getByRole("button", { name: "sales", exact: true }).click();
  const reads = state.dataReads.length;
  await main.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  await expect(main.getByRole("region", { name: "Interactive lineage graph" })).toBeVisible();
  await expect(main.getByRole("button", { name: "Upstream lineage", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(main.getByLabel("Show operational dependencies", { exact: true })).not.toBeChecked();
  expect(state.dataReads.length).toBe(reads);
  await main.getByRole("button", { name: "Open in catalog", exact: true }).click();
  await expect(main.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "Lineage", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(main.getByRole("region", { name: "Interactive lineage graph" })).toBeVisible();
  expect(state.reads.some(path => path.startsWith("/v1/catalog/assets/cat-table/lineage"))).toBe(true);
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workloads", exact: true }).click();
  await main.getByRole("button", { name: "Finance", exact: true }).click();
  await main.getByRole("button", { name: "Reports", exact: true }).click();
  await main.getByRole("button", { name: "Sales app", exact: true }).click();
  await main.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  await expect(main.getByRole("region", { name: "Interactive lineage graph" })).toBeVisible();
  expect(state.reads.some(path => path.startsWith("/v1/catalog/assets/cat-sales/lineage"))).toBe(true);
  await main.getByLabel("Show operational dependencies", { exact: true }).check();
  await main.getByLabel("Show columns", { exact: true }).check();
  await main.getByRole("button", { name: "Downstream lineage", exact: true }).click();
  await main.getByRole("button", { name: "Reset graph", exact: true }).click();
  await expect(main.getByLabel("Show operational dependencies", { exact: true })).not.toBeChecked();
  await expect(main.getByLabel("Show columns", { exact: true })).not.toBeChecked();
  await expect(main.getByRole("button", { name: "Upstream lineage", exact: true })).toHaveAttribute("aria-pressed", "true");
  expect(state.errors).toEqual([]);
});

test("catalog searches products and exposes governance SLA access and interactive lineage", async ({ page }) => {
  const state = await setup(page);
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  await expect(main.getByRole("table", { name: "Catalog results" })).toBeVisible();
  await main.getByLabel("Search the catalog", { exact: true }).fill("Sales app");
  await expect(main.getByRole("status")).toHaveText("1 objects you can access");
  await main.getByRole("button", { name: "Sales app", exact: true }).click();
  const nav = page.getByRole("navigation", { name: "Catalog sections" });
  await nav.getByRole("button", { name: "Governance", exact: true }).click();
  await main.getByLabel("Description", { exact: true }).fill("Governed finance dashboard");
  await main.getByLabel("Domain", { exact: true }).fill("Finance");
  await main.getByLabel("Tags (comma separated)", { exact: true }).fill("finance, monthly");
  await main.getByLabel("Accountable owner group", { exact: true }).selectOption("analysts");
  await main.getByLabel("Steward / contact", { exact: true }).fill("finance@example.test");
  await main.getByLabel("Classification", { exact: true }).selectOption("internal");
  await main.getByLabel("Governance status", { exact: true }).selectOption("reviewed");
  await main.getByLabel("Lineage purpose", { exact: true }).selectOption("operational");
  await main.getByRole("button", { name: "Save governance", exact: true }).click();
  await expect(main.getByRole("status")).toHaveText("Governance saved.");
  expect(state.mutations.at(-1)?.body).toMatchObject({ domain: "Finance", tags: ["finance", "monthly"], owner_group_id: "analysts", status: "reviewed", lineage_role: "operational" });
  await nav.getByRole("button", { name: "SLA", exact: true }).click();
  await main.getByLabel("Maximum data age (minutes)", { exact: true }).fill("60");
  await main.getByLabel("Minimum successful runs (%)", { exact: true }).fill("95");
  await main.getByRole("button", { name: "Save SLA", exact: true }).click();
  await expect(main.getByRole("status")).toHaveText("SLA saved.");
  await expect(main.getByRole("table", { name: "SLA checks" })).toContainText("Unknown");
  await nav.getByRole("button", { name: "Permissions", exact: true }).click();
  await expect(main.getByRole("region", { name: "Permissions for Sales app" })).toBeVisible();
  await page.getByRole("combobox", { name: "App group", exact: true }).selectOption("analysts");
  await page.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(main.getByRole("status")).toHaveText("Assigned Executor to Analysts.");
  expect(state.mutations.at(-1)?.path).toBe("/v1/rbac/resources/sales-service/assignments");
  await nav.getByRole("button", { name: "Lineage", exact: true }).click();
  await expect(main.getByRole("region", { name: "Interactive lineage graph" }).getByRole("group", { name: "Revenue, table", exact: true })).toBeVisible();
  await main.getByRole("region", { name: "Interactive lineage graph" }).getByRole("group", { name: "Daily report, script", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Daily report", exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Open entity", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Daily report", exact: true })).toBeVisible();
  await expect(main.getByRole("button", { name: "Upstream lineage", exact: true })).toHaveAttribute("aria-pressed", "true");
  await main.getByText("Filters and evidence", { exact: true }).click();
  await expect(main.getByRole("combobox", { name: "Depth", exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("catalog-lineage.png") });
  expect(state.errors).toEqual([]);
});

test("catalog readers cannot edit governance SLAs or grants and table access stays on schema", async ({ page }) => {
  const state = await setup(page, "reader");
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  await main.getByRole("button", { name: "Revenue", exact: true }).click();
  const nav = page.getByRole("navigation", { name: "Catalog sections" });
  await nav.getByRole("button", { name: "Governance", exact: true }).click();
  await expect(main.getByLabel("Description", { exact: true })).toBeDisabled();
  await expect(main.getByRole("button", { name: "Save governance", exact: true })).toHaveCount(0);
  await nav.getByRole("button", { name: "SLA", exact: true }).click();
  await expect(main.getByLabel("Maximum data age (minutes)", { exact: true })).toBeDisabled();
  await expect(main.getByRole("button", { name: "Save SLA", exact: true })).toHaveCount(0);
  await nav.getByRole("button", { name: "Permissions", exact: true }).click();
  await expect(main.getByRole("region", { name: "Permissions for reporting" })).toBeVisible();
  await expect(main.getByRole("button", { name: "Assign role", exact: true })).toHaveCount(0);
  expect(state.mutations).toEqual([]);
});

test("catalog autocomplete supports keyboard selection and the global search shortcut", async ({ page }) => {
  const state = await setup(page);
  await page.keyboard.press("Control+k");
  const search = page.getByRole("combobox", { name: "Search the catalog", exact: true });
  await expect(search).toBeFocused();
  await search.fill("Sales");
  await expect(page.getByRole("option", { name: /Sales app/ })).toBeVisible();
  await search.press("ArrowDown");
  await search.press("Enter");
  await expect(page.locator(".main").getByRole("heading", { name: "Sales app", exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("lineage groups native Airflow tasks and supports expansion columns and downstream impact", async ({ page }) => {
  const state = await setup(page), table = state.catalogItems[2], app = state.catalogItems[0];
  const dag = { ...table, id: "native-dag", name: "finance_daily", kind: "airflow_dag", attributes: { dag_id: "finance_daily" } };
  const extract = { ...table, lineage_parent_id: "native-dag", id: "native-extract", name: "extract", kind: "airflow_task", attributes: { dag_id: "finance_daily" } };
  const transform = { ...table, lineage_parent_id: "native-dag", id: "native-transform", name: "transform", kind: "airflow_task", attributes: { dag_id: "finance_daily" } };
  const archive = { ...table, id: "archive", name: "Archive" };
  const edge = (source: string, target: string, relation = "depends_on") => ({ id: source + target, source, target, relation, origin: "airflow_dag", revision: "abc1234", observed_at: "2026-10-18T12:00:00Z" });
  const requests: URL[] = [];
  await page.route("https://iam.test/api/v1/catalog/assets/*/lineage?*", async (route) => {
    const url = new URL(route.request().url()); requests.push(url);
    const expanded = url.searchParams.get("depth") === "1" && url.searchParams.get("direction") === "downstream";
    return route.fulfill({ json: { nodes: (expanded ? [table, archive] : [table, dag, extract, transform, app]).map((n, i) => ({ ...n, distance: i, upstream_count: 1, downstream_count: 1 })),
      edges: expanded ? [edge(table.id, archive.id)] : [edge(table.id, extract.id), edge(dag.id, extract.id, "contains"), edge(dag.id, transform.id, "contains"), edge(extract.id, transform.id), edge(transform.id, app.id), edge(table.id, app.id, "reads")], truncated: false } });
  });
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  await main.getByRole("button", { name: "Revenue", exact: true }).click();
  await page.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  await main.getByRole("button", { name: "Upstream & downstream", exact: true }).click();
  const graph = main.getByRole("region", { name: "Interactive lineage graph" });
  await main.getByLabel("Show orchestration", { exact: true }).check();
  const pipeline = graph.getByRole("group", { name: "finance_daily, airflow dag", exact: true });
  await expect(pipeline).toBeVisible();
  await graph.getByRole("button", { name: "Expand contents of finance_daily", exact: true }).click();
  await expect(pipeline.getByText("Pipeline", { exact: true })).toBeVisible();
  await expect.poll(async () => {
    const outer = await pipeline.boundingBox(), inner = await graph.locator('[data-id="native-extract"]').boundingBox();
    return !!outer && !!inner && inner.x >= outer.x && inner.y > outer.y && inner.x + inner.width <= outer.x + outer.width + 1 && inner.y + inner.height <= outer.y + outer.height + 1;
  }).toBe(true);
  await main.getByLabel("Show columns", { exact: true }).check();
  await expect(graph.getByRole("group", { name: "Revenue, table", exact: true }).getByText("amount", { exact: true })).toBeVisible();
  await graph.getByRole("button", { name: "Expand downstream of Revenue", exact: true }).click();
  await expect(graph.getByRole("group", { name: "Archive, table", exact: true })).toBeVisible();
  expect(requests.some((r) => r.searchParams.get("direction") === "downstream" && r.searchParams.get("depth") === "1")).toBe(true);
  await graph.getByRole("button", { name: "Collapse downstream of Revenue", exact: true }).click();
  await expect(graph.getByRole("group", { name: "Archive, table", exact: true })).toHaveCount(0);
  await main.getByLabel("Depth", { exact: true }).selectOption("3");
  await main.getByRole("button", { name: "Impact analysis", exact: true }).click();
  await expect(main.getByRole("table", { name: "Downstream impact" })).toContainText("Sales app");
  await main.getByLabel("Observed within", { exact: true }).selectOption("7");
  await expect.poll(() => requests.some((r) => r.searchParams.has("since"))).toBe(true);
  await page.screenshot({ path: test.info().outputPath("lineage-impact.png") });
  expect(state.errors).toEqual([]);
});

test("Data lists only sources and schemas with data access", async ({ page }) => {
  // A platform admin manages every source but holds a data role only on finance-dw / reporting.
  const state = await setup(page, "owner", (r) => r.id === "pg-reporting");
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "finance-dw", exact: true })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: "lake", exact: true })).toHaveCount(0);
  await expect(main.getByRole("button", { name: "lake", exact: true })).toHaveCount(0);
  await expect(main.getByRole("row").filter({ hasText: "finance-dw" }).getByRole("cell").last()).toHaveText("1");
  await sidebar.getByRole("button", { name: "Expand data source finance-dw", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "Expand schema reporting", exact: true })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: /schema_0/ })).toHaveCount(0);
  await sidebar.getByRole("button", { name: "finance-dw", exact: true }).click();
  await expect(main.getByRole("button", { name: "reporting", exact: true })).toBeVisible();
  await expect(main.getByRole("button", { name: "schema_000", exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("a source-level data role shows all of its schemas", async ({ page }) => {
  await setup(page, "reader", (r) => r.id === "source-lake" || r.parent_id === "source-lake");
  const sidebar = page.locator(".sidebar");
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "finance-dw", exact: true })).toHaveCount(0);
  await sidebar.getByRole("button", { name: "Expand data source lake", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "Expand schema reporting", exact: true })).toBeVisible();
});

test("recent runs load 20 at a time with an option to load more", async ({ page }) => {
  const state = await setup(page);
  const limits: number[] = [];
  const runs = Array.from({ length: 45 }, (_, i) => ({ id: `run-${i}`, initiated_by: "admin", owner: "admin", workspace_id: null, workload_id: "script", commit_sha: null,
    kind: "script", entrypoint: `jobs/job_${String(i).padStart(2, "0")}.py`, program: null, args: {}, runtime: "python", runtime_version: "3.12",
    status: "succeeded", pool_name: "small", created_at: "2026-10-18T09:00:00Z", finished_at: "2026-10-18T09:05:00Z" }));
  await page.route("https://iam.test/api/v1/runs?*", (route) => {
    const params = new URL(route.request().url()).searchParams, limit = Number(params.get("limit")); if (params.get("sort") !== "updated") limits.push(limit);
    return route.fulfill({ json: runs.slice(0, limit) });
  });
  await page.reload();
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  const sidebar = page.locator(".sidebar");
  await expect(sidebar.getByRole("button", { name: /jobs\/job_/ })).toHaveCount(20);
  expect(limits.at(-1)).toBe(21);
  await sidebar.getByRole("button", { name: "Load more runs", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: /jobs\/job_/ })).toHaveCount(40);
  await sidebar.getByRole("button", { name: "Load more runs", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: /jobs\/job_/ })).toHaveCount(45);
  await expect(sidebar.getByRole("button", { name: "Load more runs", exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("workspace runs are listed under Workspaces, apart from runs of deployed programs", async ({ page }) => {
  const state = await setup(page);
  const base = { initiated_by: "admin", owner: "admin", commit_sha: null, kind: "script", program: null, args: {}, runtime: "python", runtime_version: "3.12",
    status: "pending", waiting_reason: "waiting for cluster capacity: no node has enough memory free", pool_name: "small", created_at: "2026-10-18T09:00:00Z", finished_at: null };
  await page.route("https://iam.test/api/v1/runs?*", (route) => route.fulfill({ json: [
    { ...base, id: "ws-run", workspace_id: "ws-1", workload_id: null, entrypoint: "explore.py" },
    { ...base, id: "deployed-run", workspace_id: null, workload_id: "script", entrypoint: "jobs/daily.py", status: "succeeded", waiting_reason: null }] }));
  await page.reload();
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("button", { name: "Workloads", exact: true }).click();
  await expect(sidebar.getByRole("list", { name: "Recent runs" })).toContainText("jobs/daily.py");
  await expect(sidebar.getByRole("list", { name: "Recent runs" })).not.toContainText("explore.py");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  const workspaceRuns = sidebar.getByRole("list", { name: "Workspace runs" });
  await expect(workspaceRuns).toContainText("explore.py");
  await expect(workspaceRuns).not.toContainText("jobs/daily.py");
  await sidebar.getByRole("button", { name: "Workspace runs", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Workspace runs" })).toBeVisible();
  await expect(main.getByRole("table", { name: "Runs" })).toContainText("no node has enough memory free");
  const views = page.getByRole("navigation", { name: "Views" });
  await main.getByRole("table", { name: "Runs" }).getByRole("button", { name: "explore.py", exact: true }).click();
  await expect(views.getByRole("button", { name: "Workspaces", exact: true })).toHaveAttribute("aria-current", "page");  // stays in Workspaces
  await workspaceRuns.getByRole("button", { name: /explore\.py/ }).click();
  await expect(views.getByRole("button", { name: "Workspaces", exact: true })).toHaveAttribute("aria-current", "page");
  expect(state.errors).toEqual([]);
});

test("warehouse choices start with the default and never offer Trino's warehouse", async ({ page }) => {
  const state = await setup(page);
  await page.route("https://iam.test/api/v1/me", (route) => route.fulfill({ json: { username: "admin", is_admin: true, groups: [], data_sources: [], available_groups: [],
    pools: [{ id: "a", name: "small", cpu: 1, memory_gb: 2, max_concurrent: 6, auto_suspend_seconds: 600, allowed_groups: [] },
      { id: "b", name: "medium", cpu: 2, memory_gb: 2, max_concurrent: 6, auto_suspend_seconds: 600, allowed_groups: [], default: true },
      { id: "c", name: "tf-trino", cpu: 1, memory_gb: 1, max_concurrent: 6, auto_suspend_seconds: 600, allowed_groups: [], trino_layers: ["Non-Prod"] }] } }));
  await page.route("https://iam.test/api/v1/workspaces", (route) => route.fulfill({ json: [{ id: "ws-1", owner: "admin", name: "Notebook", editor: "vscode", status: "running",
    last_activity_at: "2026-10-20T10:00:00Z", created_at: "2026-10-20T10:00:00Z" }] }));
  await page.reload();
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Workspace runs", exact: true }).click();
  await page.locator(".main").getByRole("button", { name: "New run", exact: true }).click();
  const choice = page.locator(".main").getByLabel("Warehouse", { exact: true });
  await expect(choice).toHaveValue("medium");
  await expect(choice.locator("option")).toHaveText(["small · 1 vCPU · 2 GB", "medium · 2 vCPU · 2 GB (default)"]);
  expect(state.errors).toEqual([]);
});

test("warehouse choices only offer warehouses meant for the layer and kind, starting with the scoped default", async ({ page }) => {
  const state = await setup(page);
  const pool = (id: string, name: string, extra: object = {}) => ({ id, name, cpu: 1, memory_gb: 2, max_concurrent: 6, auto_suspend_seconds: 600, allowed_groups: [], ...extra });
  await page.route("https://iam.test/api/v1/me", (route) => route.fulfill({ json: { username: "admin", is_admin: true, groups: [], data_sources: [], available_groups: [],
    pools: [pool("a", "shared", { default: true }), pool("b", "dbt-only", { workload_kinds: ["dbt"] }),
      pool("c", "prod-only", { layer_ids: ["1a0000000001"], layer_names: ["Prod"] }),
      pool("d", "nonprod-scripts", { layer_ids: ["1a0000000002"], workload_kinds: ["script"],
        default_for: [{ layer_id: "1a0000000002", layer_name: "Non-Prod", workload_kind: "script" }] })] } }));
  await page.route("https://iam.test/api/v1/workspaces", (route) => route.fulfill({ json: [{ id: "ws-1", owner: "admin", name: "Notebook", editor: "vscode", status: "running",
    layer_id: "1a0000000002", layer_name: "Non-Prod", last_activity_at: "2026-10-20T10:00:00Z", created_at: "2026-10-20T10:00:00Z" }] }));
  await page.reload();
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Workspace runs", exact: true }).click();
  await page.locator(".main").getByRole("button", { name: "New run", exact: true }).click();
  const choice = page.locator(".main").getByLabel("Warehouse", { exact: true });
  await expect(choice).toHaveValue("nonprod-scripts");
  await expect(choice.locator("option")).toHaveText(["shared · 1 vCPU · 2 GB (default)", "nonprod-scripts · 1 vCPU · 2 GB"]);
  expect(state.errors).toEqual([]);
});

test("administrators limit a warehouse to layers and kinds of work", async ({ page }) => {
  const state = await setup(page);
  const saved: any[] = [];
  await page.route("https://iam.test/api/v1/admin/pools", (route) => route.fulfill({ json: [{ id: "p1", name: "etl", cpu: 4, memory_gb: 16, gpu: 0,
    max_concurrent: 6, auto_suspend_seconds: 600, allowed_groups: [], layer_ids: [], workload_kinds: [], layer_names: [], default_for: [] }] }));
  await page.route("https://iam.test/api/v1/admin/pools/*", async (route) => {
    saved.push(route.request().postDataJSON());
    await route.fulfill({ json: route.request().postDataJSON() });
  });
  await page.route("https://iam.test/api/v1/admin/settings/workspaces", (route) => route.fulfill({ json: { warehouse: null, max_running: null, max_running_per_user: 3 } }));
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Compute pools", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: /small/ }).click();
  await main.getByRole("button", { name: "Configure pools", exact: true }).click();
  await expect(main.getByRole("cell", { name: "every layer · every kind", exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Edit", exact: true }).click();
  const form = main.locator("form.card").filter({ hasText: "Edit etl" });
  await form.getByRole("group", { name: /For layers/ }).getByLabel("Prod", { exact: true }).check();
  await form.getByRole("group", { name: /For kinds of work/ }).getByLabel("dbt", { exact: true }).check();
  await form.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(() => saved.length).toBe(1);
  expect(saved[0]).toMatchObject({ name: "etl", layer_ids: ["1a0000000001"], workload_kinds: ["dbt"] });
  await expect(main.getByRole("heading", { name: "Defaults by layer and kind of work" })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("storage integrations are created by choosing a type and its required location", async ({ page }) => {
  const state = await setup(page);
  const types = [
    { kind: "postgres", label: "PostgreSQL", summary: "A PostgreSQL database.", external: "Create a Trino catalog.", secrets: [
      { name: "provisioner_secret", label: "Access-setup login", required: true, help: "", fields: [
        { name: "username", label: "Username", required: true, kind: "text", placeholder: "", help: "", default: null, secret: false },
        { name: "password", label: "Password", required: true, kind: "password", placeholder: "", help: "", default: null, secret: false }] }],
      config: [{ name: "host", label: "Host", required: true, kind: "text", placeholder: "", help: "", default: null, secret: false },
        { name: "port", label: "Port", required: true, kind: "port", placeholder: "", help: "", default: 5432, secret: false },
        { name: "database", label: "Database", required: true, kind: "text", placeholder: "", help: "", default: null, secret: false },
        { name: "catalog", label: "Trino catalog", required: true, kind: "catalog", placeholder: "", help: "", default: null, secret: false }] },
    { kind: "s3", label: "S3 files", summary: "Plain files.", external: "Create a role.", config: [], secrets: [] },
  ];
  const puts: any[] = [];
  await page.route("https://iam.test/api/v1/admin/storage-types", (route) => route.fulfill({ json: types }));
  await page.route("https://iam.test/api/v1/admin/datasources", (route) => route.fulfill({ json: puts.map((b, i) => ({ id: `ds${i}`, groups: [], egress: [], description: "", ...b })) }));
  await page.route("https://iam.test/api/v1/admin/datasources/*", async (route) => {
    const body = route.request().postDataJSON();
    if (!body.config.host) return route.fulfill({ status: 422, json: { detail: { message: "PostgreSQL integration is incomplete", errors: { host: "required" } } } });
    puts.push(body); return route.fulfill({ json: { id: "ds", groups: [], egress: [], ...body } });
  });
  await page.getByRole("button", { name: "Administration", exact: true }).click();
  const main = page.locator(".main");
  await expect(main.getByRole("heading", { name: "Storage integrations", exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Add integration", exact: true }).click();
  await page.screenshot({ path: test.info().outputPath("storage-types.png") });
  await main.getByRole("button", { name: /^PostgreSQL/ }).click();
  await expect(main.getByText("Configure outside Floren Data Platform first.")).toBeVisible();
  await expect(main.getByLabel("Port *", { exact: true })).toHaveValue("5432");
  await main.getByLabel("Name *", { exact: true }).fill("sales-dw");
  await main.getByLabel("Database *", { exact: true }).fill("sales");
  await main.getByLabel("Trino catalog *", { exact: true }).fill("sales");
  await main.getByLabel("Username *", { exact: true }).fill("gov");
  await main.getByLabel("Password *", { exact: true }).fill("secret");
  await main.getByRole("button", { name: "Create integration", exact: true }).click();
  await expect(main.getByText("required", { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("storage-form-errors.png") });
  await main.getByLabel("Host *", { exact: true }).fill("pg.internal");
  await main.getByRole("button", { name: "Create integration", exact: true }).click();
  await expect(main.getByRole("cell", { name: "pg.internal:5432/sales · catalog sales", exact: true })).toBeVisible();
  expect(puts.at(-1)).toMatchObject({ name: "sales-dw", kind: "postgres", config: { host: "pg.internal", port: 5432, database: "sales", catalog: "sales" },
    provisioner_secret: { username: "gov", password: "secret" } });
  expect(state.errors).toEqual([]);
});

test("large lineage graphs start compact and trace the selected path", async ({ page }) => {
  const state = await setup(page), table = state.catalogItems[2];
  // A chain a0 → a1 → … → a11 plus an unrelated branch b0 → … → b11.
  const node = (id: string) => ({ ...table, id, name: id, distance: 1, upstream_count: 1, downstream_count: 1 });
  const ids = [...Array.from({ length: 12 }, (_, i) => `a${i}`), ...Array.from({ length: 12 }, (_, i) => `b${i}`)];
  const edges = ids.filter((id) => !id.endsWith("11")).map((id) => {
    const next = `${id[0]}${Number(id.slice(1)) + 1}`;
    return { id: `${id}-${next}`, source: id, target: next, relation: "reads", origin: "dbt_manifest", revision: "abc", observed_at: "2026-10-18T12:00:00Z" };
  });
  await page.route("https://iam.test/api/v1/catalog/assets/*/lineage?*", (route) =>
    route.fulfill({ json: { nodes: [{ ...table, distance: 0 }, ...ids.map(node)], edges, truncated: false } }));
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  await main.getByRole("button", { name: "Revenue", exact: true }).click();
  await page.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  const graph = main.getByRole("region", { name: "Interactive lineage graph" });
  await expect(graph.getByRole("group", { name: "a5, table", exact: true })).toBeVisible();
  await expect(main.getByLabel("Compact", { exact: true })).toBeChecked();
  await expect(graph.locator(".lineage-entity.compact").first()).toBeVisible();
  await graph.getByRole("group", { name: "a5, table", exact: true }).getByText("a5", { exact: true }).click();
  await expect(graph.locator('[data-id="a0"]')).not.toHaveClass(/dimmed/);
  await expect(graph.locator('[data-id="a11"]')).not.toHaveClass(/dimmed/);
  await expect(graph.locator('[data-id="b3"]')).toHaveClass(/dimmed/);
  await page.screenshot({ path: test.info().outputPath("lineage-traced.png") });
  await main.getByRole("button", { name: "Clear path highlight", exact: true }).click();
  await expect(graph.locator('[data-id="b3"]')).not.toHaveClass(/dimmed/);
  expect(state.errors).toEqual([]);
});

test("a SQL workspace is created from the editor choice and explains where scripts live", async ({ page }) => {
  const state = await setup(page);
  const created: any[] = [];
  await page.route("https://iam.test/api/v1/workspaces", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      created.push({ id: "sqlws", owner: "admin", name: body.name, editor: body.editor, status: "stopped", last_activity_at: "2026-10-18T10:00:00Z", created_at: "2026-10-18T10:00:00Z" });
      return route.fulfill({ status: 201, json: created.at(-1) });
    }
    return route.fulfill({ json: created });
  });
  await page.route("https://iam.test/api/v1/workspaces/sqlws/start", (route) => { created[0].status = "running"; return route.fulfill({ json: created[0] }); });
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await sidebar.getByRole("button", { name: "New workspace", exact: true }).click();
  await sidebar.getByLabel("Name", { exact: true }).fill("queries");
  await sidebar.getByLabel("Editor", { exact: true }).selectOption("sql");
  await sidebar.getByRole("button", { name: "Create & start", exact: true }).click();
  await sidebar.getByRole("button", { name: /queries/ }).first().click();
  await expect(main.getByRole("heading", { name: "queries", exact: true })).toBeVisible();
  await expect(main.getByText(/SQL · CloudBeaver/)).toBeVisible();
  await expect(main.getByRole("heading", { name: "Programs", exact: true })).toHaveCount(0);
  await expect(main.getByText(/export and file upload are disabled/)).toBeVisible();
  expect(created[0]).toMatchObject({ name: "queries", editor: "sql" });
  expect(state.errors).toEqual([]);
});

test("certificates are added as secrets with validation and shown with their facts", async ({ page }) => {
  const state = await setup(page);
  const soon = new Date(Date.now() + 12 * 86400000).toISOString();
  const certificate = { subject: "CN=api.finance.test", issuer: "CN=Finance Test CA", serial: "1a2b", not_before: "2026-10-01T00:00:00Z", not_after: soon,
    sha256_fingerprint: "AB:CD:EF", subject_alt_names: ["api.finance.test"], has_private_key: true, chain_length: 1 };
  const secrets: any[] = [{ name: "api-key", ref: "me/api-key", scope: "user", owner: "admin", kind: "string", certificate: null, description: "External API", updated_at: "2026-10-17T12:00:00Z", updated_by: "admin" }];
  const puts: any[] = [];
  await page.route("https://iam.test/api/v1/secrets", (route) => route.fulfill({ json: secrets }));
  await page.route("https://iam.test/api/v1/secrets/user/me/*", (route) => {
    const body = route.request().postDataJSON(); puts.push(body);
    if (!body.private_key) return route.fulfill({ status: 422, json: { detail: { message: "certificate is not valid", errors: { certificate: "paste PEM certificates (-----BEGIN CERTIFICATE-----)" } } } });
    const saved = { name: "finance-api", ref: "me/finance-api", scope: "user", owner: "admin", kind: "certificate", certificate, description: "", updated_at: "2026-10-19T09:00:00Z", updated_by: "admin" };
    secrets.push(saved); return route.fulfill({ json: saved });
  });
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Security", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Secrets", exact: true }).click();
  await main.getByRole("button", { name: "Add secret", exact: true }).click();
  await main.getByLabel("Name", { exact: true }).fill("finance-api");
  await main.getByRole("button", { name: "Certificate", exact: true }).click();
  await main.getByLabel("Certificate *", { exact: true }).fill("garbage");
  await main.getByRole("button", { name: "Save secret", exact: true }).click();
  await expect(main.getByText("paste PEM certificates (-----BEGIN CERTIFICATE-----)")).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("certificate-form.png") });
  await main.getByLabel("Certificate *", { exact: true }).fill("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----");
  await main.getByLabel("Private key", { exact: true }).fill("-----BEGIN PRIVATE KEY-----\nMIGH\n-----END PRIVATE KEY-----");
  await main.getByRole("button", { name: "Save secret", exact: true }).click();
  const detail = main.getByRole("region", { name: "Secret finance-api" });
  await expect(detail.getByText("CN=api.finance.test")).toBeVisible();
  await expect(detail.getByText("Included (never shown)")).toBeVisible();
  await expect(detail.getByText(/Expires in 1[12] days/)).toBeVisible();
  await expect(detail.getByText(/FINANCE_API_KEY/).first()).toBeVisible();
  await expect(main.getByRole("table", { name: "Secrets" }).getByRole("row").filter({ hasText: "me/finance-api" })).toContainText("Certificate");
  expect(puts.at(-1)).toMatchObject({ kind: "certificate", certificate: expect.stringContaining("BEGIN CERTIFICATE"), private_key: expect.stringContaining("PRIVATE KEY") });
  await page.screenshot({ path: test.info().outputPath("certificate-detail.png") });
  expect(state.errors).toEqual([]);
});

test("an expired sign-in asks to sign in again instead of reporting an outage", async ({ page }) => {
  // The token is about to expire and the identity provider cannot renew it.
  await page.route("https://idp.test/**", (route) => route.fulfill({ status: 503, body: "unavailable" }));
  const state = await setup(page, "owner", undefined, 30).catch(() => null);
  const dialog = page.getByRole("alertdialog", { name: "Session expired" });
  await expect(dialog).toBeVisible();
  await expect(page.getByText(/Cannot reach Floren Data Platform/)).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Sign in again", exact: true })).toBeFocused();
  expect(state?.errors ?? []).toEqual([]);
});

test("an internal API without caller authentication is allowed for testing with a red warning", async ({ page }) => {
  const state = await setup(page);
  const apis: any[] = [];
  await page.route("https://iam.test/api/v1/apis**", async (route) => {
    const req = route.request(), method = req.method(), body = req.postDataJSON();
    const parts = new URL(req.url()).pathname.replace("/api/v1/apis", "").split("/").filter(Boolean);
    if (!parts.length && method === "GET") return route.fulfill({ json: { apis, can_create: true } });
    if (!parts.length && method === "POST") {
      const created = { ...body, id: "orders-id", resource_id: "res-orders", actions: ["read", "edit", "create", "delete", "call"], endpoint: "https://api.test/gw/orders",
        operation_count: 0, updated_at: "now", created_by: "admin", config: { inbound: body.inbound, layer_id: body.layer_id }, operations: [], groups: [] };
      apis.push(created);
      return route.fulfill({ status: 201, json: created });
    }
    return route.fulfill({ json: apis.find((a) => a.id === parts[0]) });
  });
  const main = page.locator(".main");
  await page.getByRole("button", { name: "APIs", exact: true }).click();
  await main.getByRole("button", { name: "Create internal API", exact: true }).click();
  await main.getByLabel("Name *", { exact: true }).fill("Orders");
  await main.getByLabel("Address *", { exact: true }).fill("orders");
  await main.getByLabel("Allow anonymous callers (testing only)").check();
  await expect(main.getByRole("alert")).toContainText("No caller authentication");
  await expect(main.getByLabel("Require a JWT (OAuth 2.0 / OpenID Connect bearer token)")).toBeDisabled();
  await expect(main.getByLabel("Require a JWT (OAuth 2.0 / OpenID Connect bearer token)")).not.toBeChecked();
  await main.getByRole("button", { name: "Create API", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Orders", exact: true })).toBeVisible();
  expect(apis[0].inbound).toMatchObject({ anonymous: true, jwt: { enabled: false }, mtls: { enabled: false } });
  await expect(main.getByRole("alert").first()).toContainText("No caller authentication");
  expect(state.errors).toEqual([]);
});

test("admins define group workspace templates that every member gets", async ({ page }) => {
  const state = await setup(page);
  const templates: any[] = [];
  await page.route("https://iam.test/api/v1/admin/repos", (route) => route.fulfill({ json: [{ id: "analytics", name: "analytics", url: "https://git.test/analytics" }] }));
  await page.route("https://iam.test/api/v1/admin/workspace-templates**", async (route) => {
    const req = route.request();
    if (req.method() === "POST") { templates.push({ ...req.postDataJSON(), id: "t1", group_name: "Analysts", layer_name: "Non-Prod", workspaces: 3 }); return route.fulfill({ status: 201, json: templates[0] }); }
    return route.fulfill({ json: templates });
  });
  const main = page.locator(".main"), sidebar = page.locator(".sidebar");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Administration", exact: true }).click();
  await sidebar.getByRole("button", { name: "Workspace templates", exact: true }).click();
  await expect(main.getByText("No templates yet.", { exact: false })).toBeVisible();
  await main.getByRole("button", { name: "Add template", exact: true }).click();
  const form = main.getByRole("form", { name: "Workspace template" });
  await form.getByLabel("Workspace name").fill("Exploration");
  await form.getByLabel("Group").selectOption("analysts");
  await form.getByLabel("Editor").selectOption("rstudio");
  await form.getByLabel("analytics").check();
  await form.getByRole("button", { name: "Save template", exact: true }).click();
  await expect(main.getByRole("table", { name: "Workspace templates" })).toContainText("Exploration");
  expect(templates[0]).toMatchObject({ name: "Exploration", group_id: "analysts", editor: "rstudio", layer_id: "1a0000000002", repositories: ["analytics"] });
  expect(state.errors).toEqual([]);
});

test("admins allow host names workspaces may reach", async ({ page }) => {
  const state = await setup(page);
  let saved: any = null;
  await page.route("https://iam.test/api/v1/admin/settings/egress", async (route) => {
    const req = route.request();
    if (req.method() === "PUT") { saved = req.postDataJSON(); return route.fulfill({ json: { ...saved, proxy: true } }); }
    return route.fulfill({ json: { hosts: saved?.hosts ?? [], proxy: true } });
  });
  const main = page.locator(".main"), sidebar = page.locator(".sidebar");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Administration", exact: true }).click();
  await sidebar.getByRole("button", { name: "Network allowlist", exact: true }).click();
  await expect(main.getByText("No hosts allowed", { exact: false })).toBeVisible();
  await main.getByRole("button", { name: "Add host", exact: true }).click();
  await main.getByLabel("Host 1").fill("github.com");
  await main.getByLabel("Description 1").fill("Git");
  await main.getByRole("button", { name: "Add host", exact: true }).click();
  await main.getByLabel("Host 2").fill("*.githubusercontent.com");
  await main.getByRole("button", { name: "Add host", exact: true }).click();   // left empty: not saved
  await main.getByRole("button", { name: "Save allowlist", exact: true }).click();
  await expect(main.getByRole("button", { name: "Save allowlist", exact: true })).toBeHidden();
  expect(saved).toEqual({ hosts: [{ host: "github.com", description: "Git" }, { host: "*.githubusercontent.com", description: "" }] });
  await expect(main.getByLabel("Host 2")).toHaveValue("*.githubusercontent.com");
  expect(state.errors).toEqual([]);
});

test("people keep Git credentials, whose tokens are never shown again", async ({ page }) => {
  const state = await setup(page);
  const saved: any[] = [];
  await page.route("https://iam.test/api/v1/me/git-credentials**", async (route) => {
    const req = route.request();
    if (req.method() === "POST") { saved.push(req.postDataJSON()); return route.fulfill({ status: 201, json: { id: "c1", ...req.postDataJSON(), updated_at: "now" } }); }
    if (req.method() === "PUT") { saved.push({ put: new URL(req.url()).pathname.split("/").pop(), ...req.postDataJSON() }); return route.fulfill({ json: {} }); }
    return route.fulfill({ json: saved.filter((c) => !c.put).map(({ token, ...c }, i) => ({ id: `c${i + 1}`, label: c.label || c.host, ...c, updated_at: "now" })) });
  });
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Account", exact: true }).click();
  await page.getByRole("menuitem", { name: "Git credentials", exact: true }).click();
  await expect(main.getByText("No credentials yet.", { exact: false })).toBeVisible();
  await main.getByRole("button", { name: "Add credential", exact: true }).click();
  const form = main.getByRole("form", { name: "Git credential" });
  await expect(form.getByLabel("Host")).toHaveValue("github.com");
  await form.getByLabel("Name", { exact: true }).fill("GitHub work");
  await form.getByLabel("Username").fill("ana-gh");
  await form.getByLabel("Token").fill("ghp_secret");
  await form.getByLabel("Commit as (email)").fill("ana@floren.app");
  await form.getByRole("button", { name: "Save credential", exact: true }).click();
  const table = main.getByRole("table", { name: "Git credentials" });
  await expect(table).toContainText("GitHub work");
  await expect(table).toContainText("ana@floren.app");
  await expect(main).not.toContainText("ghp_secret");
  expect(saved[0]).toMatchObject({ label: "GitHub work", host: "github.com", username: "ana-gh", token: "ghp_secret", commit_email: "ana@floren.app" });
  // Editing without a token keeps the saved one.
  await table.getByRole("button", { name: "Edit", exact: true }).click();
  await expect(form.getByLabel("Token (empty keeps the saved one)")).toHaveValue("");
  await form.getByLabel("Username").fill("ana-work");
  await form.getByRole("button", { name: "Save credential", exact: true }).click();
  await expect.poll(() => saved.at(-1)).toMatchObject({ put: "c1", username: "ana-work", token: "" });
  expect(state.errors).toEqual([]);
});

test("the first start of a workspace asks which Git credential it uses", async ({ page }) => {
  const state = await setup(page);
  const ws = { id: "w1", owner: "admin", name: "Analysis", editor: "vscode", status: "stopped", git_asked: false, git_credential_id: null,
    last_activity_at: "2026-10-24T10:00:00Z", created_at: "2026-10-24T10:00:00Z" };
  const calls: string[] = [];
  await page.route("https://iam.test/api/v1/workspaces", (route) => route.fulfill({ json: [ws] }));
  await page.route("https://iam.test/api/v1/me/git-credentials", (route) => {
    if (route.request().method() === "POST") { calls.push("create " + route.request().postDataJSON().label); return route.fulfill({ status: 201, json: { id: "c9" } }); }
    return route.fulfill({ json: [{ id: "c1", label: "GitHub work", host: "github.com", username: "ana-gh", commit_name: "", commit_email: "ana@floren.app", updated_at: "now" },
      { id: "c2", label: "GitHub personal", host: "github.com", username: "ana", commit_name: "", commit_email: "", updated_at: "now" }] });
  });
  await page.route("https://iam.test/api/v1/workspaces/w1/git", (route) => {
    calls.push("git " + route.request().postDataJSON().credential_id);
    Object.assign(ws, { git_asked: true, git_credential_id: route.request().postDataJSON().credential_id });
    return route.fulfill({ json: ws });
  });
  await page.route("https://iam.test/api/v1/workspaces/w1/start", (route) => { calls.push("start"); return route.fulfill({ json: { ...ws, status: "starting" } }); });
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  const sidebar = page.locator(".sidebar");
  await sidebar.getByRole("button", { name: "Start", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Git for Analysis" });
  await expect(dialog).toContainText("GitHub personal");
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(calls).toEqual([]);                                   // cancelled: not started
  await sidebar.getByRole("button", { name: "Start", exact: true }).click();
  await dialog.getByLabel(/GitHub work/).check();
  await dialog.getByRole("button", { name: "Use and start", exact: true }).click();
  await expect.poll(() => calls).toEqual(["git c1", "start"]);
  // Asked once: the next start goes straight through; a new credential can be added from the question too.
  Object.assign(ws, { git_asked: false, status: "stopped" }); calls.length = 0;
  await sidebar.getByRole("button", { name: "Refresh", exact: true }).first().click();
  await sidebar.getByRole("button", { name: "Start", exact: true }).click();
  await dialog.getByLabel("Add a new credential").check();
  await dialog.getByLabel("Name", { exact: true }).fill("GitLab");
  await dialog.getByLabel("Host").fill("gitlab.com");
  await dialog.getByLabel("Username").fill("ana");
  await dialog.getByLabel("Token").fill("glpat");
  await dialog.getByRole("button", { name: "Use and start", exact: true }).click();
  await expect.poll(() => calls).toEqual(["create GitLab", "git c9", "start"]);
  expect(state.errors).toEqual([]);
});

test("people find packages, Publishers import from PyPI and publish their own, owners yank versions", async ({ page }) => {
  const state = await setup(page);
  const imports: any[] = [], uploads: string[] = [], yanks: any[] = [];
  const pkg = { id: "p1", kind: "python", name: "fdp-tools", key: "fdp-tools", origin: "internal", summary: "Team helpers", latest: "1.1.0", versions: 2,
    owner_group: "Analysts", resource_id: "res-p1", created_by: "ana", created_at: "2026-10-24T10:00:00Z" };
  const file = (v: string) => ({ filename: `fdp_tools-${v}-py3-none-any.whl`, size: 2048, sha256: "ab", requires_python: ">=3.9", yanked: false, yanked_reason: "", uploaded_by: "ana", created_at: "2026-10-24T10:00:00Z" });
  await page.route("https://iam.test/api/v1/packages**", async (route) => {
    const req = route.request(), path = new URL(req.url()).pathname.replace("/api/v1/packages", "");
    if (path === "" ) return route.fulfill({ json: { packages: [pkg, { ...pkg, id: "p2", kind: "r", name: "dplyr", key: "dplyr", origin: "cran", summary: "", owner_group: null, resource_id: null, latest: "1.1.4", versions: 1 }],
      actions: ["create", "import"], resource_id: "packages", upstream: { python: true, r: true }, max_upload_mb: 200 } });
    if (path === "/imports" && req.method() === "POST") { imports.push(req.postDataJSON()); return route.fulfill({ status: 202, json: { id: "i1", status: "queued", ...req.postDataJSON() } }); }
    if (path === "/imports") return route.fulfill({ json: imports.map((b, i) => ({ id: `i${i}`, ...b, status: "done", requested_by: "ana", log: "Python 3.12 · x86_64: resolved\nAdded 3 file(s).", added: ["a.whl", "b.whl", "c.whl"], created_at: "2026-10-24T10:00:00Z", finished_at: null })) });
    if (path === "/python/upload") { uploads.push(req.headers()["content-type"] ?? ""); return route.fulfill({ status: 201, json: { package: pkg, created: true } }); }
    if (path.endsWith("/yank")) { yanks.push({ path, ...req.postDataJSON() }); }
    return route.fulfill({ json: { ...pkg, actions: ["publish", "yank", "delete", "assign"], install: "pip install fdp-tools", version_count: 2,
      versions: [{ version: "1.1.0", yanked: false, requires: ["requests>=2"], install: "pip install fdp-tools==1.1.0", files: [file("1.1.0")] },
                 { version: "1.0.0", yanked: false, requires: [], install: "pip install fdp-tools==1.0.0", files: [file("1.0.0")] }] } });
  });
  const main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Packages", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "All packages", exact: true }).click();
  const table = main.getByRole("table", { name: "Packages" });
  await expect(table).toContainText("fdp-tools");
  await expect(table).toContainText("CRAN");
  await main.getByLabel("Search packages").fill("dpl");
  await expect(table).not.toContainText("fdp-tools");
  await main.getByRole("button", { name: "Import from PyPI or CRAN", exact: true }).click();
  const form = main.getByRole("form", { name: "Import a package" });
  await form.getByLabel("Package").fill("pandas==2.2.3");
  await form.getByRole("button", { name: "Import", exact: true }).click();
  await expect(main.getByRole("table", { name: "Package imports" })).toContainText("pandas==2.2.3");
  expect(imports).toEqual([{ kind: "python", requirement: "pandas==2.2.3" }]);
  await page.locator(".sidebar").getByRole("button", { name: "All packages", exact: true }).click();
  await main.getByRole("button", { name: "Publish a package", exact: true }).click();
  const publish = main.getByRole("form", { name: "Publish a package" });
  await publish.getByLabel("File").setInputFiles({ name: "fdp_tools-1.1.0-py3-none-any.whl", mimeType: "application/zip", buffer: Buffer.from("PK") });
  await publish.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(main.getByRole("table", { name: "Versions" })).toContainText("fdp_tools-1.0.0-py3-none-any.whl");
  expect(uploads[0]).toContain("multipart/form-data");
  await expect(main.getByText("pip install fdp-tools", { exact: true })).toBeVisible();
  page.once("dialog", (d) => d.accept("broken import"));
  await main.getByRole("table", { name: "Versions" }).getByRole("button", { name: "Yank" }).first().click();
  await expect.poll(() => yanks).toEqual([{ path: "/python/fdp-tools/versions/1.1.0/yank", reason: "broken import" }]);
  expect(state.errors).toEqual([]);
});

test("an installation without R offers no RStudio and no R packages", async ({ page }) => {
  const state = await setup(page);
  await page.route("https://iam.test/api/v1/me", (route) => route.fulfill({ json: { username: "admin", is_admin: true, active_group_id: null,
    available_groups: [], groups: [], pools: [], data_sources: [], features: { r: false } } }));
  await page.route("https://iam.test/api/v1/packages**", (route) => route.fulfill({ json: { packages: [], actions: ["create", "import"],
    resource_id: "packages", upstream: { python: true, r: false }, kinds: ["python"], max_upload_mb: 200 } }));
  await page.reload();
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await sidebar.getByRole("button", { name: "New workspace", exact: true }).click();
  await expect(sidebar.getByLabel("Editor")).toBeVisible();
  expect(await sidebar.getByLabel("Editor").locator("option").allTextContents()).not.toContain("RStudio");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Packages", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: "Python", exact: true })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: "R", exact: true })).toHaveCount(0);
  await sidebar.getByRole("button", { name: "All packages", exact: true }).click();
  await main.getByRole("button", { name: "Publish a package", exact: true }).click();
  expect(await main.getByLabel("Language").locator("option").allTextContents()).toEqual(["Python"]);
  expect(state.errors).toEqual([]);
});

test("administrators see who or what ran each query and set the query log's retention", async ({ page }) => {
  const state = await setup(page);
  const requests: string[] = [], saved: any[] = [];
  const row = { id: "r1", layer_id: "1a0000000002", query_id: "20261025_q1", actor: "ana", principal_id: "p1", run_id: "", workload_id: "wl1",
    workspace_id: "", trino_user: "p1", source: "fdp", query_type: "SELECT", state: "FAILED", error: "TABLE_NOT_FOUND Table lake.sales.x does not exist",
    sql_text: null, tables: ["lake.sales.orders"], output_table: "", rows: 1200, bytes: 2097152, cpu_ms: 250, wall_ms: 1500, queued_ms: 0,
    created_at: "2026-10-25T10:00:00Z", ended_at: "2026-10-25T10:00:01Z" };
  await page.route("https://iam.test/api/v1/query-log**", (route) => { requests.push(new URL(route.request().url()).search); return route.fulfill({ json: [row] }); });
  await page.route("https://iam.test/api/v1/admin/settings/query-log", (route) => {
    if (route.request().method() === "PUT") { saved.push(route.request().postDataJSON()); return route.fulfill({ json: route.request().postDataJSON() }); }
    return route.fulfill({ json: { retention_days: 90, record_sql: false } });
  });
  const main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Administration", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "Query log", exact: true }).click();
  const table = main.getByRole("table", { name: "Queries" });
  await expect(table).toContainText("ana");
  await expect(table).toContainText("workload wl1");
  await expect(table).toContainText("lake.sales.orders");
  await expect(table).toContainText("Failed");
  await expect(table).toContainText("1.5 s");
  await main.getByLabel("Table").fill("orders");
  await expect.poll(() => requests.at(-1)).toContain("table=orders");
  const form = main.getByRole("form", { name: "Query log settings" });
  await form.getByLabel("Keep queries for (days)").fill("30");
  await form.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect.poll(() => saved).toEqual([{ retention_days: 30, record_sql: false }]);
  expect(state.errors).toEqual([]);
});

test("administrators keep the list of public packages that may be imported", async ({ page }) => {
  const state = await setup(page);
  const saved: any[] = [];
  await page.route("https://iam.test/api/v1/packages**", (route) => {
    const req = route.request(), path = new URL(req.url()).pathname;
    if (path.endsWith("/allowlist") && req.method() === "PUT") { saved.push(req.postDataJSON()); return route.fulfill({ json: req.postDataJSON() }); }
    if (path.endsWith("/allowlist")) return route.fulfill({ json: { enabled: false, python: [{ name: "numpy", description: "" }], r: [] } });
    return route.fulfill({ json: { packages: [], actions: ["create", "import"], resource_id: "packages", upstream: { python: true, r: true }, kinds: ["python", "r"], max_upload_mb: 200 } });
  });
  const main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Packages", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "All packages", exact: true }).click();
  await main.getByRole("button", { name: "Allowed packages", exact: true }).click();
  const panel = main.getByRole("region", { name: "Allowed packages" });
  await expect(panel.getByLabel("Python package 1")).toHaveValue("numpy");
  await panel.getByLabel("Only allow listed packages").check();
  await panel.getByRole("button", { name: "Add Python package", exact: true }).click();
  await panel.getByLabel("Python package 2").fill("pandas");
  await panel.getByLabel("Python description 2").fill("Data frames");
  await panel.getByRole("button", { name: "Add R package", exact: true }).click();
  await panel.getByLabel("R package 1").fill("DBI");
  await panel.getByRole("button", { name: "Save allowed packages", exact: true }).click();
  await expect.poll(() => saved).toEqual([{ enabled: true, python: [{ name: "numpy", description: "" }, { name: "pandas", description: "Data frames" }], r: [{ name: "DBI", description: "" }] }]);
  expect(state.errors).toEqual([]);
});

test("API management creates external APIs with grouped operations and tries them", async ({ page }) => {
  const state = await setup(page);
  const apis: any[] = [];
  const actions = ["read", "call", "edit", "create", "delete", "assign", "identity"];
  const egress: any[] = [];
  await page.route("https://iam.test/api/v1/apis**", async (route) => {
    const req = route.request(), url = new URL(req.url()), method = req.method(), body = req.postDataJSON();
    const parts = url.pathname.replace("/api/v1/apis", "").split("/").filter(Boolean);
    if (!parts.length && method === "GET") return route.fulfill({ json: { apis, can_create: true } });
    if (!parts.length && method === "POST") {
      if (!body.outbound.base_url) return route.fulfill({ status: 422, json: { detail: { message: "API configuration is incomplete", errors: { "outbound.base_url": "enter the backend's http(s) base URL" } } } });
      const created = { ...body, id: "crm-id", resource_id: "res-crm", actions, endpoint: `/v1/egress/${body.slug}`, operation_count: 0, updated_at: "now", created_by: "admin", config: { outbound: body.outbound }, operations: [], groups: [] };
      apis.push(created);
      return route.fulfill({ status: 201, json: created });
    }
    const item = apis.find((a) => a.id === parts[0]);
    if (parts.length === 1 && method === "PUT") { Object.assign(item, { config: { outbound: body.outbound }, updated_at: "later" }); return route.fulfill({ json: item }); }
    if (parts.length === 1) return route.fulfill({ json: item });
    if (parts[1] === "operations" && method === "POST") {
      const op = { ...body, id: "op-1", resource_id: "res-op", actions, config: body.external };
      item.operations.push(op); item.operation_count = 1; item.groups = [{ name: body.group, resource_id: "res-group", actions }];
      return route.fulfill({ status: 201, json: op });
    }
    return route.fulfill({ status: 404, json: { detail: "unexpected" } });
  });
  await page.route("https://iam.test/api/v1/egress/**", (route) => {
    egress.push({ method: route.request().method(), url: route.request().url() });
    return route.fulfill({ json: { id: 7, name: "Acme" }, headers: { "Access-Control-Allow-Origin": "*" } });
  });
  const main = page.locator(".main"), sidebar = page.locator(".sidebar");
  await page.getByRole("button", { name: "APIs", exact: true }).click();
  await expect(main.getByRole("heading", { name: "API management", exact: true })).toBeVisible();
  await expect(main.getByRole("navigation", { name: "API directions" }).getByRole("button", { name: /Internal/ })).toHaveAttribute("aria-current", "page");
  await main.getByRole("navigation", { name: "API directions" }).getByRole("button", { name: /External/ }).click();
  await main.getByRole("button", { name: "Create external API", exact: true }).click();
  await main.getByLabel("Name *", { exact: true }).fill("CRM");
  await main.getByLabel("Address *", { exact: true }).fill("crm");
  await main.getByLabel("Type", { exact: true }).selectOption("bearer");
  await expect(main.getByLabel("Token secret", { exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Create API", exact: true }).click();
  await expect(main.getByText("enter the backend's http(s) base URL")).toBeVisible();
  await main.getByLabel("Base URL *", { exact: true }).fill("https://api.crm.example");
  await main.getByRole("button", { name: "Create API", exact: true }).click();
  await expect(main.getByRole("heading", { name: "CRM", exact: true })).toBeVisible();
  await expect(sidebar.getByRole("button", { name: /CRM/ })).toBeVisible();
  // Add an operation in an endpoint group, then try it.
  await main.getByLabel("Name *", { exact: true }).fill("get-customer");
  await main.getByLabel("Endpoint group", { exact: true }).fill("Customers");
  await main.getByLabel("Path", { exact: true }).fill("/customers/{id}");
  await main.getByRole("button", { name: "Add operation", exact: true }).last().click();
  await expect(main.getByRole("region", { name: "Try it" })).toBeVisible();
  await main.getByLabel("Path", { exact: true }).last().fill("/customers/7");
  await main.getByRole("button", { name: "Send", exact: true }).click();
  await expect(main.getByRole("region", { name: "Try it" })).toContainText('"name": "Acme"');
  expect(egress.at(-1)?.url).toBe("https://iam.test/api/v1/egress/crm/customers/7");
  await page.screenshot({ path: test.info().outputPath("api-operations.png") });
  await main.getByRole("navigation", { name: "API sections" }).getByRole("button", { name: "Settings", exact: true }).click();
  await expect(main.getByLabel("Base URL *", { exact: true })).toHaveValue("https://api.crm.example");
  await page.screenshot({ path: test.info().outputPath("api-settings.png") });
  // List fields keep what is typed until saved.
  await main.getByLabel("Request headers from programs", { exact: true }).pressSequentially("X-Correlation-Id, Accept-Language");
  await expect(main.getByLabel("Request headers from programs", { exact: true })).toHaveValue("X-Correlation-Id, Accept-Language");
  await main.getByRole("button", { name: "Save settings", exact: true }).click();
  await expect(main.getByRole("status")).toHaveText("Settings saved.");
  expect(apis[0].config.outbound.pass_request_headers).toEqual(["X-Correlation-Id", "Accept-Language"]);
  expect(state.errors).toEqual([]);
});

test("workspace creation selects a layer and excludes Prod when workspaces are disabled", async ({ page }) => {
  const state = await setup(page);
  state.resources.push(resource("test-env", "folder", "Pokemon Test", "workloads", { environment: true, workspaces: true, branch: "test" }));
  state.resources.push(resource("prod-env", "folder", "Pokemon Prod", "workloads", { environment: true, workspaces: false, branch: "main" }));
  await page.locator(".sidebar").getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "New workspace", exact: true }).click();
  const select = page.locator(".sidebar").getByLabel("Layer", { exact: true });
  await expect(select).toHaveValue("1a0000000002");
  await expect(select.locator("option")).toHaveText(["Non-Prod"]);
  await expect(page.getByText("Access requires this layer and your own object permissions. No workspace security group is created.")).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("lineage keeps data scope and overlays only relevant orchestration on demand", async ({ page }) => {
  const state = await setup(page), app = state.catalogItems[0], dataset = state.catalogItems[2];
  const node = (id: string, name: string, kind: string) => ({ ...dataset, id, name, kind, distance: -1, upstream_count: 1, downstream_count: 1 });
  const raw = node("raw-cards", "Raw cards", "table"), apiNode = node("tcgdex", "TCGdex Prod", "api"), ingest = node("ingest", "Card ingestion", "script"), model = node("model", "Readable cards", "dbt_model"), testNode = node("test", "unique_card_id", "dbt_test");
  const requests: URL[] = [];
  await page.route("https://iam.test/api/v1/catalog/assets/*/lineage?*", async (route) => {
    const url = new URL(route.request().url()); requests.push(url);
    const orchestration = url.searchParams.get("orchestration") === "true";
    const operational = url.searchParams.get("operational") === "true";
    const checkpoint = node("checkpoint", "load_commits", "table");
    const dag = node("dag", "pokemon_daily", "airflow_dag"), task = { ...node("task", "ingest_cards", "airflow_task"), lineage_parent_id: "dag" };
    const project = node("project", "Card models", "dbt"), dbtTask = { ...node("dbt-task", "build_models", "airflow_task"), lineage_parent_id: "dag" };
    const nodes = [apiNode, ingest, raw, { ...model, ...(orchestration ? { lineage_parent_id: "project" } : {}) }, dataset, { ...app, distance: 0 }, ...(orchestration ? [dag, task, project, dbtTask] : []), ...(operational ? [checkpoint] : [])];
    const ids = [apiNode.id, ingest.id, raw.id, model.id, dataset.id, app.id];
    const edges = ids.slice(1).map((id, i) => ({ id: `step${i}`, source: ids[i], target: id, relation: "reads", origin: "dbt_manifest", revision: "sha", observed_at: "2026-10-19T12:00:00Z" }));
    const control = (source: string, target: string) => ({ id: source + target, source, target, relation: "triggers", origin: "airflow_dag", revision: "sha", observed_at: "2026-10-19T12:00:00Z", control: true });
    return route.fulfill({ json: { nodes, edges: [...edges, ...(operational ? [{ ...control(checkpoint.id, ingest.id), relation: "reads", origin: "trino_runtime", category: "operational" }] : []), ...(orchestration ? [control(task.id, ingest.id), control(dbtTask.id, project.id)] : [])], truncated: false } });
  });
  const main = page.locator(".main");
  await page.getByRole("button", { name: "Catalog", exact: true }).click();
  await main.getByRole("button", { name: "Sales app", exact: true }).click();
  await page.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "Lineage", exact: true }).click();
  await expect(main.getByRole("button", { name: "Upstream lineage", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => requests.some((r) => r.searchParams.get("view") === "flow" && r.searchParams.get("direction") === "upstream" && r.searchParams.get("depth") === "16")).toBe(true);
  const graph = main.getByRole("region", { name: "Interactive lineage graph" });
  for (const [name, kind] of [["TCGdex Prod", "api"], ["Card ingestion", "script"], ["Raw cards", "table"], ["Readable cards", "dbt model"], ["Revenue", "table"], ["Sales app", "app"]]) {
    await expect(graph.getByRole("group", { name: `${name}, ${kind}`, exact: true })).toBeVisible();
  }
  await expect(graph.getByText("unique_card_id", { exact: true })).toHaveCount(0);
  await main.getByRole("navigation", { name: "Jump to lineage stage" }).getByRole("button", { name: /^APIs/ }).click();
  await expect.poll(async () => {
    const viewport = await graph.boundingBox(), node = await graph.getByRole("group", { name: "TCGdex Prod, api", exact: true }).boundingBox();
    return !!viewport && !!node && node.x >= viewport.x && node.x + node.width <= viewport.x + viewport.width;
  }).toBe(true);
  await main.getByRole("button", { name: "Upstream & downstream", exact: true }).click();
  await expect.poll(() => requests.some((r) => r.searchParams.get("view") === "flow" && r.searchParams.get("direction") === "both")).toBe(true);
  await expect(graph.getByText("unique_card_id", { exact: true })).toHaveCount(0);
  await expect(main.getByLabel("Show operational dependencies", { exact: true })).not.toBeChecked();
  await expect(graph.locator('[data-id="checkpoint"]')).toHaveCount(0);
  await main.getByLabel("Show operational dependencies", { exact: true }).check();
  await expect(graph.locator('[data-id="checkpoint"]')).toBeVisible();
  await expect(graph.locator('[data-id="checkpointingest"] .react-flow__edge-path')).toHaveCSS("stroke-dasharray", "5px, 4px");
  await expect(graph.locator('[data-id="checkpointingest"] .react-flow__edge-path')).toHaveCSS("stroke", "rgb(100, 116, 139)");
  await expect(graph.getByText("reads · operational", { exact: true })).toBeVisible();
  await main.getByLabel("Show operational dependencies", { exact: true }).uncheck();
  await expect(graph.locator('[data-id="checkpoint"]')).toHaveCount(0);
  await expect(main.getByLabel("Show orchestration", { exact: true })).not.toBeChecked();
  await expect(graph.locator('[data-id="dag"]')).toHaveCount(0);
  await main.getByLabel("Show orchestration", { exact: true }).check();
  await expect(graph.locator('[data-id="dag"]')).toBeVisible();
  await main.getByRole("navigation", { name: "Jump to lineage stage" }).getByRole("button", { name: /^dbt/ }).click();
  await graph.getByRole("button", { name: "Expand contents of Card models", exact: true }).click();
  await main.getByRole("button", { name: "Airflow orchestration", exact: true }).click();
  await graph.getByRole("button", { name: "Expand contents of pokemon_daily", exact: true }).click();
  await expect(graph.locator('[data-id="project"]').getByText("dbt project", { exact: true })).toBeVisible();
  await expect(graph.locator('[data-id="model"]')).toHaveClass(/react-flow__node/);
  await page.screenshot({path: test.info().outputPath("expanded-orchestration.png")});
  await expect(graph.locator('[data-id="taskingest"] .react-flow__edge-path')).toHaveCSS("stroke-dasharray", "5px, 4px");
  await expect(graph.getByText("triggers", { exact: true })).toHaveCount(2);
  await main.getByLabel("Show orchestration", { exact: true }).uncheck();
  await expect(graph.locator('[data-id="dag"]')).toHaveCount(0);
  await expect(graph.locator(".react-flow__node")).toHaveCount(6);
  expect(requests.every((r) => r.searchParams.get("view") === "flow")).toBe(true);
  await main.getByRole("button", { name: "Upstream lineage", exact: true }).click();
  await expect(graph.getByText("unique_card_id", { exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("Home starts with scoped activity and submits global search into the catalog", async ({ page }) => {
  const state = await setup(page);
  const run = { id: "new-run", initiated_by: "admin", owner: "admin", workspace_id: null, workload_id: "script", commit_sha: null,
    kind: "script", entrypoint: "daily.py", program: "new-job", args: {}, runtime: "python", runtime_version: "3.12", status: "running", pool_name: "small", created_at: "2026-10-19T10:00:00Z", finished_at: null };
  await page.route("https://iam.test/api/v1/runs?*", (route) => route.fulfill({ json: new URL(route.request().url()).searchParams.get("sort") === "updated" ? [{ ...run, id: "older-run", program: "older-job", workload_id: null, status: "succeeded", created_at: "2026-10-18T10:00:00Z", finished_at: "2026-10-19T11:00:00Z" }, run] : [run] }));
  await page.route("https://iam.test/api/v1/workspaces", (route) => route.fulfill({ json: [{ id: "dev-ws", owner: "admin", name: "Pokemon Test workspace", editor: "vscode", status: "running", last_activity_at: "2026-10-19T11:00:00Z", created_at: "2026-10-18T10:00:00Z" }] }));
  await page.reload();
  const main = page.locator(".main"), nav = page.getByRole("navigation", { name: "Views" });
  await expect(main.getByRole("tab", { name: "Home", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(nav.locator(":scope > button").first()).toHaveAttribute("aria-label", /Home/); // The first section is Home.
  expect(await nav.locator(":scope > button").evaluateAll((nodes) => nodes.map((n) => n.getAttribute("aria-label")))).toEqual(["Home", "Workspaces", "Workloads", "Storage", "Kafka", "APIs", "Packages", "Catalog", "Security", "Compute pools", "Documentation", "Administration"]);
  await expect(page.getByRole("button", { name: "Runs & apps", exact: true })).toHaveCount(0);
  await expect(main.getByRole("region", { name: "Your workspaces", exact: true })).toContainText("Pokemon Test workspace");
  await expect(main.getByRole("region", { name: "Recent runs", exact: true }).locator("li").first()).toContainText("Daily report");
  await expect(main.getByRole("region", { name: "Recently updated runs", exact: true }).locator("li").first()).toContainText("older-job");
  const search = main.getByRole("combobox", { name: "Search across Floren Data Platform", exact: true });
  await search.fill("Revenue"); await search.press("Enter");
  await expect(main.getByRole("combobox", { name: "Search the catalog", exact: true })).toHaveValue("Revenue");
  await expect(main.getByRole("table", { name: "Catalog results" })).toContainText("Revenue");
  await expect(nav.getByRole("button", { name: "Catalog", exact: true })).toHaveAttribute("aria-current", "page");
  expect(state.errors).toEqual([]);
});

test("Workloads contains the run sidebar and a searchable history tab", async ({ page }) => {
  const state = await setup(page);
  const run = { id: "run-1", initiated_by: "admin", owner: "admin", workspace_id: null, workload_id: "script", commit_sha: null,
    kind: "script", entrypoint: "daily.py", program: "daily-report", args: {}, runtime: "python", runtime_version: "3.12", status: "failed", pool_name: "small", created_at: "2026-10-19T10:00:00Z", finished_at: "2026-10-19T11:00:00Z" };
  await page.route("https://iam.test/api/v1/runs?*", (route) => route.fulfill({ json: [run] }));
  await page.reload();
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workloads", exact: true }).click();
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await expect(sidebar.getByRole("button", { name: /daily-report/ })).toBeVisible();
  expect(await sidebar.locator(".object-tree").first().evaluate((tree) => !!(tree.compareDocumentPosition(document.querySelector(".sidebar-section-divider")!) & Node.DOCUMENT_POSITION_FOLLOWING))).toBe(true);
  await main.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Recent runs", exact: true }).click();
  await expect(main.getByRole("table", { name: "Runs", exact: true })).toContainText("daily-report");
  await main.getByRole("combobox", { name: "Run status", exact: true }).selectOption("succeeded");
  await expect(main.getByText("No loaded runs match these filters.", { exact: true })).toBeVisible();
  await main.getByRole("combobox", { name: "Run status", exact: true }).selectOption("failed");
  await expect(main.getByRole("table", { name: "Runs", exact: true })).toContainText("daily-report");
  await main.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Contents", exact: true }).click();
  await expect(main.getByRole("heading", { name: "Workloads", exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("Home follows selected orchestrators and DAGs and preserves the choices after reload", async ({ page }) => {
  const state = await setup(page);
  for (const [id, title, actions] of [["test-airflow", "Test pipelines", allActions], ["prod-airflow", "Prod pipelines", allActions], ["hidden-airflow", "Restricted pipelines", ["read"]]] as const) {
    state.workloads.push({ ...state.workloads[0], id, title, kind: "airflow", actions: [...actions] });
  }
  const requests: URL[] = [];
  await page.route("https://iam.test/api/v1/workloads/*/orchestrator-runs?*", (route) => {
    const url = new URL(route.request().url()); requests.push(url);
    return route.fulfill({ json: { dags: ["cards", "prices"], runs: [{ dag_id: url.searchParams.get("dag_id") || "cards", run_id: "scheduled__2026-10-19", state: "success", logical_date: "2026-10-19T10:00:00Z", start_date: "2026-10-19T10:00:00Z", end_date: "2026-10-19T10:05:00Z" }] } });
  });
  await page.reload();
  const main = page.locator(".main");
  await expect(main.getByRole("article", { name: "Test pipelines", exact: true })).toContainText("cards");
  await expect(main.getByRole("article", { name: "Prod pipelines", exact: true })).toContainText("success");
  await expect(main.getByRole("article", { name: "Restricted pipelines", exact: true })).toHaveCount(0);
  await main.getByRole("combobox", { name: "DAG for Test pipelines", exact: true }).selectOption("prices");
  await expect.poll(() => requests.some((u) => u.pathname.includes("test-airflow") && u.searchParams.get("dag_id") === "prices")).toBe(true);
  await main.getByRole("button", { name: "Choose orchestrators", exact: true }).click();
  await main.getByRole("checkbox", { name: /Prod pipelines/ }).uncheck();
  await expect(main.getByRole("article", { name: "Prod pipelines", exact: true })).toHaveCount(0);
  const count = requests.length;
  await page.reload();
  await expect(main.getByRole("combobox", { name: "DAG for Test pipelines", exact: true })).toHaveValue("prices");
  await expect(main.getByRole("article", { name: "Prod pipelines", exact: true })).toHaveCount(0);
  expect(requests.slice(count).every((u) => u.pathname.includes("test-airflow"))).toBe(true);
  expect(requests.every((u) => !u.pathname.includes("hidden-airflow"))).toBe(true);
  // No further metadata polling after leaving Home.
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Storage", exact: true }).click();
  expect(state.dataReads).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("navigation replaces the preview page and only pinned tabs stay open", async ({ page }) => {
  const state = await setup(page), nav = page.getByRole("navigation", { name: "Views" }), tabs = page.getByRole("tablist", { name: "Open editors" });
  await nav.getByRole("button", { name: "Storage", exact: true }).click();
  await expect(tabs.getByRole("tab")).toHaveCount(1);
  await expect(tabs.getByRole("tab", { name: "Home", exact: true })).toHaveCount(0);
  await nav.getByRole("button", { name: "Catalog", exact: true }).click();
  await tabs.getByRole("button", { name: "Pin Catalog", exact: true }).click();
  await expect(tabs.getByRole("button", { name: "Unpin Catalog", exact: true })).toHaveAttribute("aria-pressed", "true");
  await nav.getByRole("button", { name: "Workloads", exact: true }).click();
  await expect(tabs.getByRole("tab")).toHaveCount(2);
  await nav.getByRole("button", { name: "APIs", exact: true }).click();
  await expect(tabs.getByRole("tab")).toHaveCount(2);
  await expect(tabs.getByRole("tab", { name: "Catalog", exact: true })).toBeVisible();
  await expect(tabs.getByRole("tab", { name: "Workloads", exact: true })).toHaveCount(0);
  await tabs.getByRole("button", { name: "Unpin Catalog", exact: true }).click();
  await expect(tabs.getByRole("tab")).toHaveCount(1);
  await expect(tabs.getByRole("tab", { name: "Catalog", exact: true })).toHaveAttribute("aria-selected", "true");
  await nav.getByRole("button", { name: "Home", exact: true }).click();
  await expect(tabs.getByRole("tab", { name: "Catalog", exact: true })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("pin state survives object section changes and pinned tabs can be closed", async ({ page }) => {
  const state = await setup(page), main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Catalog", exact: true }).click();
  await main.getByRole("button", { name: "Sales app", exact: true }).click();
  await main.getByRole("button", { name: "Pin Sales app", exact: true }).click();
  await page.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "SLA", exact: true }).click();
  await expect(main.getByRole("button", { name: "Unpin Sales app", exact: true })).toBeVisible();
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Storage", exact: true }).click();
  await main.getByRole("tab", { name: "Sales app", exact: true }).click();
  await expect(page.getByRole("navigation", { name: "Catalog sections" }).getByRole("button", { name: "SLA", exact: true })).toHaveAttribute("aria-current", "page");
  await main.getByRole("button", { name: "Close Sales app", exact: true }).click();
  await expect(main.getByRole("tablist", { name: "Open editors" }).getByRole("tab")).toHaveCount(1);
  await expect(page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Storage", exact: true })).toHaveAttribute("aria-current", "page");
  expect(state.errors).toEqual([]);
});

test("admins configure a layer and workspace creation uses that layer without a group", async ({ page }) => {
  const state = await setup(page), main = page.locator(".main"), sidebar = page.locator(".sidebar");
  const layers: any[] = [{ id: "1a0000000001", name: "Prod", description: "", workspaces: false, production: true }, { id: "1a0000000002", name: "Non-Prod", description: "", workspaces: true, default: true }];
  const created: any[] = [];
  await page.route("https://iam.test/api/v1/layers", (route) => route.fulfill({ json: layers }));
  await page.route("https://iam.test/api/v1/admin/layers", (route) => {
    const body = route.request().postDataJSON(); layers.push({ id: "1a0000000003", ...body, default: false });
    return route.fulfill({ status: 201, json: layers.at(-1) });
  });
  await page.route("https://iam.test/api/v1/workspaces", (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON(); created.push(body);
      return route.fulfill({ status: 201, json: { id: "lab-ws", ...body, layer_name: "Lab", owner: "admin", status: "stopped" } });
    }
    return route.fulfill({ json: [] });
  });
  await page.route("https://iam.test/api/v1/workspaces/lab-ws/start", (route) => route.fulfill({ json: { id: "lab-ws", status: "running" } }));
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Administration", exact: true }).click();
  await sidebar.getByRole("button", { name: "Layers", exact: true }).click();
  await main.getByRole("button", { name: "Add layer", exact: true }).click();
  await main.getByRole("textbox", { name: "Layer name", exact: true }).fill("Lab");
  await main.getByLabel("Production layer").check();
  await main.getByRole("button", { name: "Save layer", exact: true }).click();
  await expect(main.getByRole("table", { name: "Layers", exact: true })).toContainText("Lab");
  expect(layers.at(-1)).toMatchObject({ name: "Lab", production: true });
  const defaults: unknown[] = [];
  await page.route("https://iam.test/api/v1/admin/settings/default-layer", (route) => {
    defaults.push(route.request().postDataJSON());
    for (const layer of layers) layer.default = layer.id === "1a0000000003";
    return route.fulfill({ json: { layer_id: "1a0000000003", configured: true } });
  });
  await main.getByRole("row", { name: /Lab/ }).getByRole("button", { name: "Make default", exact: true }).click();
  await expect.poll(() => defaults).toEqual([{ layer_id: "1a0000000003" }]);
  await expect(main.getByRole("row", { name: /Lab/ })).toContainText("default");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await sidebar.getByRole("button", { name: "New workspace", exact: true }).click();
  await sidebar.getByLabel("Layer", { exact: true }).selectOption("1a0000000003");
  await sidebar.getByLabel("Name", { exact: true }).fill("Lab exploration");
  await sidebar.getByRole("button", { name: "Create & start", exact: true }).click();
  await expect.poll(() => created.length).toBe(1);
  expect(created[0]).toEqual({ name: "Lab exploration", editor: "vscode", layer_id: "1a0000000003" });
  expect(state.mutations.filter((m) => m.path.startsWith("/v1/rbac/groups"))).toEqual([]);
  expect(state.errors).toEqual([]);
});

test("schema Owners grant a person access directly and see User as the recipient", async ({ page }) => {
  const state = await setup(page);
  await page.route("https://iam.test/api/v1/rbac/resources/*/user-principals", (route) => route.fulfill({ json: [{ id: "ana", name: "Ana" }] }));
  await openSchema(page);
  const main = page.locator(".main");
  await main.getByRole("combobox", { name: "Grant to", exact: true }).selectOption("user");
  await main.getByRole("combobox", { name: "User", exact: true }).selectOption("ana");
  await main.getByRole("combobox", { name: "Role", exact: true }).selectOption("Contributor");
  await main.getByRole("button", { name: "Assign role", exact: true }).click();
  await expect(main.getByRole("status")).toHaveText("Assigned Contributor to Ana.");
  await expect(main.getByText("User · direct", { exact: true })).toBeVisible();
  expect(state.mutations.at(-1)).toEqual({ method: "PUT", path: "/v1/rbac/resources/pg-reporting/assignments", body: { principal_id: "ana", role: "Contributor" } });
  expect(state.errors).toEqual([]);
});

test("active group stays at the sidebar bottom across tabs and reloads", async ({ page }) => {
  const state = await setup(page), picker=page.getByRole("combobox",{name:"Active group",exact:true});
  await expect(picker).toHaveValue("analysts");
  await picker.selectOption("operators");
  await expect(picker).toHaveValue("operators");
  expect(state.mutations.some((m)=>m.method==="PUT" && m.path==="/v1/me/active-group" && m.body.group_id==="operators")).toBeTruthy();
  for(const name of ["Catalog","Workloads","Storage","Security"]) {
    await page.getByRole("navigation",{name:"Views"}).getByRole("button",{name,exact:true}).click();
    await expect(picker).toBeVisible(); await expect(picker).toHaveValue("operators");
  }
  const before=await picker.boundingBox();
  await page.locator(".sidebar-content").evaluate((el)=>{el.scrollTop=el.scrollHeight;});
  expect((await picker.boundingBox())?.y).toBe(before?.y);
  await page.reload(); await expect(picker).toHaveValue("operators");
  expect(state.errors).toEqual([]);
});

test("a rejected active group switch preserves the previous selection", async ({ page }) => {
  const state = await setup(page);
  await page.route("https://iam.test/api/v1/me/active-group",route=>route.fulfill({status:403,json:{detail:"Group membership is required"}}));
  await page.getByRole("combobox",{name:"Active group",exact:true}).selectOption("operators");
  await expect(page.getByText("Group membership is required",{exact:true})).toBeVisible();
  await expect(page.getByRole("combobox",{name:"Active group",exact:true})).toHaveValue("analysts");
  expect(state.errors).toEqual([]);
});

test("active group switching does not wait for slow runtime lists", async ({ page }) => {
  const state=await setup(page);
  let release: () => void = () => {};
  const pending=new Promise<void>((resolve)=>{release=resolve;});
  await page.route("https://iam.test/api/v1/runs?*",async route=>{await pending;await route.fallback();});
  try {
    const picker=page.getByRole("combobox",{name:"Active group",exact:true});
    await picker.selectOption("operators");
    await expect(picker).toBeEnabled();
    await expect(picker).toHaveValue("operators");
    expect(state.errors).toEqual([]);
  } finally {release();}
});

test("Global Admin is shown as protected on object IAM and cannot be removed", async ({ page }) => {
  const state = await setup(page);
  await page.route("https://iam.test/api/v1/rbac/resources/pg-reporting/assignments",route=>route.fulfill({json:[{
    id:"global",resource_id:"pg-reporting",app_group_id:"000000000001",group:"Global Admin",role:"Owner",inherited:false,system:true}]}));
  await openSchema(page);
  await expect(page.locator(".main").getByText("System · protected",{exact:true})).toBeVisible();
  await expect(page.getByRole("button",{name:"Remove Global Admin's direct Owner role",exact:true})).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("switching group refreshes storage and catalog without reusing the previous scope", async ({ page }) => {
  const state=await setup(page);
  await page.route("https://iam.test/api/v1/rbac/resources",route=>route.fulfill({json:state.resources.filter(r=>state.activeGroup()==="analysts" || !r.id.startsWith("pg-") && r.id!=="source-pg")}));
  await page.route("https://iam.test/api/v1/catalog/search?*",route=>route.fulfill({json:{
    items:state.activeGroup()==="analysts"?state.catalogItems:state.catalogItems.filter(a=>a.id==="cat-script"),
    total:state.activeGroup()==="analysts"?3:1,offset:0,limit:30,facets:{}}}));
  const nav=page.getByRole("navigation",{name:"Views"});
  const main=page.locator(".main"),picker=page.getByLabel("Active group",{exact:true});
  await openSchema(page);
  expect(state.dataReads.length).toBeGreaterThan(0);
  await nav.getByRole("button",{name:"Catalog",exact:true}).click();
  await expect(main.getByRole("table",{name:"Catalog results"})).toContainText("Revenue");
  await picker.selectOption("operators");
  await expect(picker).toBeEnabled();
  await expect(main.getByRole("table",{name:"Catalog results"})).toContainText("Daily report");
  await expect(main.getByRole("table",{name:"Catalog results"})).not.toContainText("Revenue");
  await nav.getByRole("button",{name:"Storage",exact:true}).click();
  await expect(main.getByRole("table")).not.toContainText("finance-dw");
  await expect(main.getByRole("table")).toContainText("lake");
  const reads=state.dataReads.length;
  await picker.selectOption("analysts");
  await expect(picker).toBeEnabled();
  await openSchema(page);
  await expect.poll(()=>state.dataReads.length).toBeGreaterThan(reads);
  expect(state.errors).toEqual([]);
});

test("SQL workspace waits for readiness and explains startup failures", async ({ page }) => {
  const state=await setup(page);
  const ws={id:"waiting-sql",owner:"admin",name:"Queries",editor:"sql",status:"starting",startup_error:null as string|null,
    started_at:"2026-10-20T10:00:00Z",last_activity_at:"2026-10-20T10:00:00Z",created_at:"2026-10-20T10:00:00Z"};
  await page.route("https://iam.test/api/v1/workspaces",route=>route.fulfill({json:[ws]}));
  const sidebar=page.locator(".sidebar"),main=page.locator(".main");
  await page.getByRole("navigation",{name:"Views"}).getByRole("button",{name:"Workspaces",exact:true}).click();
  await sidebar.getByRole("button",{name:"Refresh",exact:true}).click();
  await sidebar.getByRole("button",{name:/^Queries/}).click();
  await expect(main.getByRole("status")).toContainText("editor is preparing");
  await expect(main.getByRole("button",{name:"Starting…",exact:true})).toBeDisabled();
  await expect(main.getByRole("button",{name:"Stop",exact:true})).toBeEnabled();
  ws.status="failed";ws.startup_error="The editor did not become ready within 300 seconds.";
  await sidebar.getByRole("button",{name:"Refresh",exact:true}).click();
  await expect(main.getByRole("alert")).toContainText(ws.startup_error);
  await expect(main.getByRole("button",{name:"Retry start",exact:true})).toBeEnabled();
  await expect(main.getByRole("button",{name:"Open editor",exact:true})).toHaveCount(0);
  ws.status="running";ws.startup_error=null;
  await sidebar.getByRole("button",{name:"Refresh",exact:true}).click();
  await expect(main.getByRole("button",{name:"Open editor",exact:true})).toBeEnabled();
  await expect(main.getByRole("status")).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("queued work shows its place in line and admins set workspace limits", async ({ page }) => {
  const state = await setup(page);
  const ws = { id: "waiting-ws", owner: "admin", name: "Notebook", editor: "vscode", status: "queued", queue_position: 2,
    started_at: null, last_activity_at: "2026-10-20T10:00:00Z", created_at: "2026-10-20T10:00:00Z" };
  const run = { id: "run-1", owner: "admin", workspace_id: "waiting-ws", workload_id: null, commit_sha: null, kind: "script", entrypoint: "etl.py",
    program: null, args: {}, runtime: "python", runtime_version: "3.12", status: "queued", queue_position: 1, pool_name: "small",
    created_at: "2026-10-20T10:01:00Z", finished_at: null, initiated_by: "admin" };
  const settings = { warehouse: null as string | null, max_running: null as number | null, max_running_per_user: 3, running: 4, queued: 1 };
  await page.route("https://iam.test/api/v1/workspaces", (route) => route.fulfill({ json: [ws] }));
  await page.route("https://iam.test/api/v1/runs**", (route) => route.fulfill({ json: [run] }));
  await page.route("https://iam.test/api/v1/admin/pools", (route) => route.fulfill({ json: [{ id: "small", name: "small", cpu: 2, memory_gb: 4, gpu: 0,
    max_concurrent: 6, auto_suspend_seconds: 600, allowed_groups: [], in_use: 6, queued: 1 }] }));
  const savedPools: unknown[] = [];
  await page.route("https://iam.test/api/v1/admin/pools/*", async (route) => {
    savedPools.push(route.request().postDataJSON());
    await route.fulfill({ json: route.request().postDataJSON() });
  });
  await page.route("https://iam.test/api/v1/admin/settings/workspaces", async (route) => {
    if (route.request().method() === "PUT") Object.assign(settings, route.request().postDataJSON());
    await route.fulfill({ json: settings });
  });
  const sidebar = page.locator(".sidebar"), main = page.locator(".main");
  await page.getByRole("navigation", { name: "Views" }).getByRole("button", { name: "Workspaces", exact: true }).click();
  await sidebar.getByRole("button", { name: "Refresh", exact: true }).click();
  await sidebar.getByRole("button", { name: /^Notebook/ }).click();
  await expect(main.getByRole("status")).toContainText("Waiting for a free workspace slot · 2nd in line");
  await expect(main.getByRole("button", { name: "Open editor", exact: true })).toBeDisabled();
  await expect(main.getByRole("button", { name: "Leave the line", exact: true })).toBeEnabled();
  await expect(main.getByRole("table", { name: "Runs" })).toContainText("queued · 1st in line");

  await page.getByRole("button", { name: "Compute pools", exact: true }).click();
  await sidebar.getByRole("button", { name: /small/ }).click();
  await main.getByRole("button", { name: "Configure pools", exact: true }).click();
  await expect(main.getByRole("cell", { name: "6 / 6 · 1 waiting", exact: true })).toBeVisible();
  await main.getByRole("button", { name: "Add pool", exact: true }).click();
  await main.getByLabel("Name").fill("data-eng");
  await main.getByLabel(/Dedicated to these groups/).check();
  await expect(main.getByLabel("Max concurrent")).toBeDisabled();
  await expect(main.getByRole("button", { name: "Save", exact: true }).first()).toBeDisabled();  // a dedicated warehouse needs groups
  await main.getByLabel(/Allowed groups/).fill("data-engineers");
  await main.getByRole("button", { name: "Save", exact: true }).first().click();
  await expect.poll(() => savedPools.at(-1)).toMatchObject({ name: "data-eng", dedicated: true, allowed_groups: ["data-engineers"] });
  const form = main.getByRole("form", { name: "Workspace limits" });
  await expect(form).toContainText("4 running, 1 waiting");
  await form.getByLabel("Editor size (warehouse)").selectOption("small");
  await form.getByLabel("Running at once (everyone)").fill("5");
  await form.getByLabel("Per person").fill("2");
  await form.getByRole("button", { name: "Save", exact: true }).click();
  await expect(form.getByRole("status")).toHaveText("Saved");
  expect(state.mutations.length).toBe(0);  // routed by this test, not the shared mock
  expect(settings).toMatchObject({ warehouse: "small", max_running: 5, max_running_per_user: 2 });
  expect(state.errors).toEqual([]);
});

test("Prod is selectable for workspace creation when enabled by its layer policy", async ({ page }) => {
  const state = await setup(page), sidebar = page.locator('.sidebar');
  await page.route('https://iam.test/api/v1/layers', route => route.fulfill({json:[
    {id:'1a0000000002',name:'Non-Prod',description:'',workspaces:true,default:true},
    {id:'1a0000000001',name:'Prod',description:'',workspaces:true,production:true},
  ]}));
  let created: any = null;
  await page.route('https://iam.test/api/v1/workspaces', route => {
    if(route.request().method() !== 'POST') return route.fulfill({json:[]});
    created = route.request().postDataJSON();
    return route.fulfill({status:201,json:{id:'prod-ws',...created,owner:'admin',status:'stopped',layer_name:'Prod'}});
  });
  await page.route('https://iam.test/api/v1/workspaces/prod-ws/start', route => route.fulfill({json:{id:'prod-ws',status:'running'}}));
  await page.reload();
  await page.getByRole('navigation',{name:'Views'}).getByRole('button',{name:'Workspaces',exact:true}).click();
  await sidebar.getByRole('button',{name:'New workspace',exact:true}).click();
  await expect(sidebar.getByLabel('Layer',{exact:true})).toHaveValue('1a0000000002');
  await sidebar.getByLabel('Layer',{exact:true}).selectOption('1a0000000001');
  await sidebar.getByLabel('Name',{exact:true}).fill('Prod analysis');
  await sidebar.getByRole('button',{name:'Create & start',exact:true}).click();
  await expect.poll(()=>created).toEqual({name:'Prod analysis',editor:'vscode',layer_id:'1a0000000001'});
  expect(state.errors).toEqual([]);
});

test('catalog owners define and measure a KPI and inspect its history',async({page})=>{
  const state=await setup(page),main=page.locator('.main');
  const metrics:any[]=[];
  await page.route('https://iam.test/api/v1/catalog/assets/cat-table/metrics',route=>{
    if(route.request().method()==='POST'){
      const body=route.request().postDataJSON();
      metrics.push({id:'coverage',asset_id:'cat-table',...body,definition:body,origin:'manual',version:1,latest:null});
      return route.fulfill({status:201,json:metrics[0]});
    }
    return route.fulfill({json:metrics});
  });
  const observation={id:'measurement',observed_at:'2026-10-20T10:00:00Z',value:98.7,status:'at_risk',definition_version:1,origin:'trino',revision:'',run_id:'',detail:{}};
  await page.route('https://iam.test/api/v1/catalog/assets/cat-table/metrics/coverage/measure',route=>{metrics[0].latest=observation;return route.fulfill({json:observation});});
  await page.route('https://iam.test/api/v1/catalog/assets/cat-table/metrics/coverage/history',route=>route.fulfill({json:[observation]}));
  await page.getByRole('navigation',{name:'Views'}).getByRole('button',{name:'Catalog',exact:true}).click();
  await main.getByRole('button',{name:'Revenue',exact:true}).click();
  await page.getByRole('navigation',{name:'Catalog sections'}).getByRole('button',{name:'KPIs & quality',exact:true}).click();
  await main.getByRole('button',{name:'Add definition',exact:true}).click();
  await main.getByLabel('Definition key',{exact:true}).fill('coverage');
  await main.getByLabel('Name',{exact:true}).fill('Card coverage');
  await main.getByLabel('Unit',{exact:true}).fill('%');
  await main.getByLabel('SQL source',{exact:true}).fill('finance-dw');
  await main.getByLabel('SQL calculation (optional)',{exact:true}).fill('SELECT 98.7');
  await main.getByRole('button',{name:'Save definition',exact:true}).click();
  await main.getByRole('button',{name:'Measure Card coverage',exact:true}).click();
  await expect(main.getByText('98.7',{exact:true})).toBeVisible();
  await main.getByText('Measurement history',{exact:true}).click();
  await expect(main.getByRole('table',{name:'History of Card coverage'})).toContainText('98.7');
  expect(metrics[0].definition.sql).toBe('SELECT 98.7');
  expect(state.errors).toEqual([]);
});

test('SLA policies support schedules and quality requirements with alert acknowledgement',async({page})=>{
  const state=await setup(page),main=page.locator('.main');
  const incident={id:'late-data',asset_id:'cat-table',status:'breached',opened_at:'2026-10-20T05:16:00Z',updated_at:'2026-10-20T05:16:00Z',resolved_at:null,acknowledged_at:null as string|null,detail:{}};
  await page.route('https://iam.test/api/v1/catalog/alerts?asset_id=cat-table',route=>route.fulfill({json:[incident]}));
  await page.route('https://iam.test/api/v1/catalog/alerts/late-data/acknowledge',route=>{incident.acknowledged_at='2026-10-20T06:00:00Z';return route.fulfill({json:{id:incident.id}});});
  await page.getByRole('navigation',{name:'Views'}).getByRole('button',{name:'Catalog',exact:true}).click();
  await main.getByRole('button',{name:'Revenue',exact:true}).click();
  await page.getByRole('navigation',{name:'Catalog sections'}).getByRole('button',{name:'SLA',exact:true}).click();
  await main.getByLabel('Daily deadline',{exact:true}).fill('07:00');
  await main.getByLabel('Timezone',{exact:true}).fill('Europe/Amsterdam');
  await main.getByLabel('Require current passing quality checks',{exact:true}).check();
  await main.getByRole('button',{name:'Save SLA',exact:true}).click();
  await expect(main.getByRole('status')).toHaveText('SLA saved.');
  expect(state.mutations.find(m=>m.path==='/v1/catalog/assets/cat-table/sla')?.body).toMatchObject({deadline_time:'07:00',timezone:'Europe/Amsterdam',quality_required:true,grace_minutes:15});
  await main.getByRole('button',{name:'Acknowledge alert',exact:true}).click();
  await expect(main.getByText(/Acknowledged/)).toBeVisible();
  expect(state.errors).toEqual([]);
});


test("slow resource discovery does not block identity or workloads", async ({ page }) => {
  await setup(page);
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route("https://iam.test/api/v1/rbac/resources", async (route) => { await pending; await route.fallback(); });
  try {
    await page.reload();
    await expect(page.getByRole("combobox", { name: "Active group", exact: true })).toHaveValue("analysts");
    await expect(page.locator(".main").getByText("Sales app", { exact: true })).toBeVisible();
  } finally { release(); }
});

test("slow runtime polling never overlaps or repeats identity each tick", async ({ page }) => {
  await page.clock.install();
  const state = await setup(page);
  await expect(page.locator(".main").getByText("Sales app", { exact: true })).toBeVisible();
  let release!: () => void;
  let requests = 0;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  await page.route("https://iam.test/api/v1/workloads", async (route) => {
    requests += 1; await pending; await route.fallback();
  });
  const identityCount = state.reads.filter((path) => path === "/v1/me").length;
  try {
    await page.clock.runFor(6000);
    await expect.poll(() => requests).toBe(1);
    await page.clock.fastForward(20000);
    expect(requests).toBe(1);
    expect(state.reads.filter((path) => path === "/v1/me").length).toBe(identityCount);
  } finally { release(); }
});

test("hidden tabs suspend hub polling and resume when visible", async ({ page }) => {
  await page.clock.install();
  const state = await setup(page);
  await expect(page.locator(".main").getByText("Sales app", { exact: true })).toBeVisible();
  await page.evaluate(() => Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }));
  const count = state.reads.filter((path) => path === "/v1/workloads").length;
  await page.clock.runFor(15000);
  expect(state.reads.filter((path) => path === "/v1/workloads").length).toBe(count);
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect.poll(() => state.reads.filter((path) => path === "/v1/workloads").length).toBeGreaterThan(count);
});

test("explicit refresh supersedes a slower earlier snapshot", async ({ page }) => {
  const state = await setup(page);
  await expect(page.locator(".main").getByText("Sales app", { exact: true })).toBeVisible();
  let release!: () => void;
  const pending = new Promise<void>((resolve) => { release = resolve; });
  let requests = 0;
  await page.route("https://iam.test/api/v1/workloads", async (route) => {
    const snapshot = JSON.parse(JSON.stringify(state.workloads));
    requests += 1;
    if (requests === 1) await pending;
    await route.fulfill({ json: snapshot, headers: { "Access-Control-Allow-Origin": "*" } });
  });
  try {
    await page.locator(".main").getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(() => requests).toBe(1);
    state.workloads[0].title = "Updated sales";
    await page.locator(".main").getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(page.locator(".main").getByText("Updated sales", { exact: true })).toBeVisible();
    release();
    await expect.poll(() => requests).toBe(2);
    await expect(page.locator(".main").getByText("Sales app", { exact: true })).toHaveCount(0);
  } finally { release(); }
});

test("schema owners mask columns and filter rows; readers see the rules", async ({ page }) => {
  const state = await setup(page);
  await openSchema(page);
  const sections = page.getByRole("navigation", { name: "Object sections" });
  await sections.getByRole("button", { name: "Masks and filters", exact: true }).click();
  const panel = page.getByRole("region", { name: /Masks and filters/ });
  await expect(panel).toContainText("No masks or filters");
  await panel.getByRole("button", { name: "Add mask or filter" }).click();
  await page.getByLabel("Table", { exact: true }).fill("Customers");
  await page.getByLabel("Column", { exact: true }).fill("email");
  await page.getByLabel("What people see").selectOption("partial");
  await page.getByRole("checkbox", { name: "Analysts" }).check();
  await page.getByLabel("Exempt identities").selectOption("etl-identity");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  const row = panel.getByRole("row", { name: /customers/ });
  await expect(row).toContainText("email");
  await expect(row).toContainText("Show the last 4 characters");
  await expect(row).toContainText("Analysts, finance-etl");
  expect(state.mutations.find((m) => m.path === "/v1/data-policies")!.body).toMatchObject({ schema_id: "pg-reporting", table: "Customers",
    kind: "column_mask", column: "email", method: "partial", expression: "", exempt_groups: ["analysts"], exempt_principals: ["etl-identity"] });

  await panel.getByRole("button", { name: "Add mask or filter" }).click();
  await page.getByLabel("Table", { exact: true }).fill("customers");
  await page.getByLabel("Kind").selectOption("row_filter");
  await expect(page.getByLabel("Column", { exact: true })).toHaveCount(0);
  await page.getByLabel("Rows people see (condition)").fill("region = 'EU' OR is_member('Analysts')");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(panel.getByRole("row", { name: /Rows/ })).toContainText("region = 'EU' OR is_member('Analysts')");

  await panel.getByRole("row", { name: /email/ }).getByRole("button", { name: "Change" }).click();
  await page.getByLabel("What people see").selectOption("hash");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(panel.getByRole("row", { name: /email/ })).toContainText("SHA-256");
  page.once("dialog", (dialog) => dialog.accept());
  await panel.getByRole("row", { name: /email/ }).getByRole("button", { name: "Remove" }).click();
  await expect(panel.getByRole("row", { name: /email/ })).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("schema readers see masks and filters but cannot change them", async ({ page }) => {
  await setup(page, "reader");
  await page.route("https://iam.test/api/v1/data-policies?*", (route) => route.fulfill({ json: [{ id: "p1", schema_id: "pg-reporting", table: "customers",
    kind: "column_mask", column: "email", method: "redact", expression: "", exempt_groups: [], exempt_principals: [], member_groups: [],
    description: "Personal data", updated_by: "admin", updated_at: null }] }));
  await page.getByRole("button", { name: "Storage", exact: true }).click();
  await page.locator(".sidebar").getByRole("button", { name: "finance-dw", exact: true }).click();
  await page.locator(".main").getByRole("button", { name: "reporting", exact: true }).click();
  await page.getByRole("navigation", { name: "Object sections" }).getByRole("button", { name: "Masks and filters", exact: true }).click();
  const panel = page.getByRole("region", { name: /Masks and filters/ });
  await expect(panel.getByRole("row", { name: /customers/ })).toContainText("Show ****");
  await expect(panel).toContainText("Personal data");
  await expect(panel.getByRole("button", { name: "Add mask or filter" })).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Change" })).toHaveCount(0);
});
