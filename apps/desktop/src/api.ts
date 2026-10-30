import { accessToken, getServer } from "./auth";

export interface Layer {
  id: string; name: string; description: string; workspaces: boolean;
  /** Personal and group secrets are not usable here. */
  production?: boolean;
  /** Where work without a layer goes (configured, or the only layer). */
  default?: boolean;
}
/** The layer a form starts with: the default layer, or nothing so the person chooses. */
export const defaultLayerId = (layers: Layer[]) => layers.find((l) => l.default)?.id ?? "";

export interface DataSource {
  id: string;
  name: string;
  kind: string;
  description: string;
  config: Record<string, unknown>;
  egress: string[];
  groups: string[];
}

export interface StorageField {
  name: string; label: string; required: boolean; kind: "text" | "port" | "catalog" | "bucket" | "s3uri" | "url" | "arn" | "choice" | "password";
  placeholder: string; help: string; default: string | number | null; secret: boolean;
}
/** An integration type and the settings it requires (served by the control plane). */
export interface StorageType {
  kind: "postgres" | "iceberg" | "s3"; label: string; summary: string; external: string;
  config: StorageField[];
  secrets: { name: "secret" | "provisioner_secret"; label: string; required: boolean; help: string; fields: StorageField[] }[];
}

export interface DirectoryPrincipal {
  id: string; kind: "user" | "group" | "role" | "service"; name: string;
  active: boolean; provider: string; external_id: string; managed: boolean;
  origin?: "app" | "idp"; provisioning?: "scim" | "oidc" | "app"; display_name?: string;
  member_of?: string[]; members?: string[]; app_groups?: string[]; platform_admin?: boolean;
}
export interface AppGroup {
  system?: boolean;
  protected_bindings?: string[];
  id: string; name: string; description: string; owners: string[]; bindings: string[]; can_manage: boolean; can_inspect?: boolean;
  origin?: "app";
}
export interface DataPolicy {
  id: string; schema_id: string; table: string; kind: "column_mask" | "row_filter"; column: string;
  method: "" | "null" | "redact" | "hash" | "partial" | "expression"; expression: string;
  exempt_groups: string[]; exempt_principals: string[]; member_groups: string[];
  description: string; updated_by: string; updated_at: string | null;
}
export type DataPolicyChange = Pick<DataPolicy, "method" | "expression" | "exempt_groups" | "exempt_principals" | "description">;
export interface AccessResource {
  owner_group_id?: string | null; owner_group_name?: string | null;
  layer_id?: string | null; layer_name?: string | null;
  id: string; name: string; kind: "root" | "folder" | "service" | "source" | "schema" | "api-root" | "api" | "api-group" | "api-operation" | "package-root" | "package";
  parent_id: string | null; object_id: string | null; config: Record<string, unknown>; actions: string[];
  /** Sources and schemas: a data role here/on a parent, or protected Global Admin access. */
  data_access?: boolean;
}

/** API management (see services/control-plane/app/apis.py). */
export type ApiDirection = "internal" | "external";
export interface ApiValue { name: string; value: string; secret: string }
export interface InboundConfig {
  jwt: { enabled: boolean; issuer: string; audience: string; jwks_uri: string; required_claims: Record<string, string>; algorithms: string[] };
  mtls: { enabled: boolean; ca_secret: string; allowed_subjects: string[]; allowed_thumbprints: string[] };
  soap_version: "1.1" | "1.2";
  /** Testing only: no caller authentication. Not allowed in production layers. */
  anonymous?: boolean;
}
export interface OutboundConfig {
  base_url: string; soap_version: "1.1" | "1.2"; headers: ApiValue[]; query: ApiValue[];
  auth: { type: "none" | "api_key" | "basic" | "bearer" | "oauth2"; name: string; location: "header" | "query"; username: string; secret: string; token_url: string; client_id: string; scope: string };
  client_certificate: string; server_ca: string; verify_tls: boolean; timeout_seconds: number;
  pass_request_headers: string[]; pass_response_headers: string[];
}
export interface ApiOperation {
  id: string; name: string; group: string; method: string; path: string; soap_action: string; description: string;
  config: { workload_id?: string; mode?: "async" | "sync"; timeout_seconds?: number | null; args?: Record<string, string>; pool?: string; headers?: ApiValue[] };
  resource_id: string; actions: string[];
}
export interface ApiDefinition {
  id: string; slug: string; name: string; direction: ApiDirection; protocol: "rest" | "soap"; description: string;
  config: { inbound?: InboundConfig; outbound?: OutboundConfig; layer_id?: string; environment_id?: string }; resource_id: string; actions: string[]; endpoint: string;
  operation_count: number; updated_at: string; created_by: string;
  identity?: { principal_id: string; name: string; username: string };
  operations?: ApiOperation[]; groups?: { name: string; resource_id: string; actions: string[] }[];
}
export interface ApiCallRecord { id: string; operation: string; caller: string; status_code: number; run_id: string | null; run_status: string | null; at: string }
export interface ApiInput {
  layer_id?: string;
  environment_id?: string;
  name: string; slug: string; direction: ApiDirection; protocol: "rest" | "soap"; description: string;
  inbound?: InboundConfig; outbound?: OutboundConfig;
}
export interface OperationInput {
  name: string; group: string; method: string; path: string; soap_action: string; description: string;
  internal?: { workload_id: string; mode: "async" | "sync"; timeout_seconds: number; args: Record<string, string>; pool: string };
  external?: { headers: ApiValue[]; timeout_seconds: number | null };
}

/** Calls an external API operation as yourself (needs Consumer), returning the raw response for the test console. */
export async function tryExternal(slug: string, method: string, path: string, body: string, headers: Record<string, string>) {
  const started = performance.now();
  const res = await fetch(`${getServer()}/api/v1/egress/${slug}/${path.replace(/^\//, "")}`, {
    method, body: method === "GET" || method === "DELETE" || !body ? undefined : body,
    headers: { ...headers, Authorization: `Bearer ${await accessToken()}` },
  });
  return { status: res.status, headers: Object.fromEntries(res.headers.entries()), body: await res.text(), ms: Math.round(performance.now() - started) };
}

/** Data a user can use. Servers that predate the flag omit it; their listing is already scoped. */
const usable = (r: AccessResource) => r.data_access !== false;
/** Schemas of a source that the user has data access to. */
export const accessibleSchemas = (resources: AccessResource[], sourceId: string) =>
  resources.filter((r) => r.kind === "schema" && r.parent_id === sourceId && usable(r)).sort((a, b) => a.name.localeCompare(b.name));
/** Sources with data access, directly or through at least one of their schemas. */
export const accessibleSources = (resources: AccessResource[]) =>
  resources.filter((r) => r.kind === "source" && (usable(r) || accessibleSchemas(resources, r.id).length > 0))
    .sort((a, b) => a.name.localeCompare(b.name));
export interface RoleAssignment {
  system?: boolean;
  id: string; resource_id: string; app_group_id: string | null; group: string | null; role: string; inherited: boolean;
  principal_id?: string | null; principal_name?: string | null; principal_active?: boolean; principal_kind?: "user" | "service";
}
export interface ServicePrincipal { id: string; name: string }

export interface DataChild {
  name: string; kind: "table" | "view" | "materialized_view" | "column" | "folder" | "file";
  data_type?: string; nullable?: boolean; ordinal_position?: number; path?: string; size?: number; modified_at?: string;
}
export interface DataChildren { children: DataChild[]; next_cursor: string | null }
export interface DataLocation { schemaId: string; table?: string; path?: string; item?: DataChild }

export type WorkloadKind = "script" | "notebook" | "app" | "dbt" | "airflow";
export const WORKLOAD_KINDS: WorkloadKind[] = ["script", "notebook", "app", "dbt", "airflow"];

export interface Pool {
  id: string;
  name: string;
  cpu: number;
  memory_gb: number;
  gpu: number;
  max_concurrent: number;
  auto_suspend_seconds: number;
  allowed_groups: string[];
  /** A team's own warehouse: only allowed_groups use it, no slot limit, never queues. */
  dedicated?: boolean;
  /** Work without a warehouse runs here, and warehouse choices start with it. */
  default?: boolean;
  /** Layers whose Trino workers run here: only Trino uses this warehouse. */
  trino_layers?: string[];
  /** Scope: the layers (ids) and kinds of work this warehouse is for; empty = every layer / kind. */
  layer_ids?: string[]; layer_names?: string[];
  workload_kinds?: WorkloadKind[];
  /** Scoped defaults that point here. */
  default_for?: { layer_id: string | null; layer_name: string | null; workload_kind: WorkloadKind | null }[];
  usage_available?: boolean; active_runs?: number | null; active_services?: number | null;
  active_executions?: number | null; active_products?: number | null;
  /** Containers counted against max_concurrent (jobs, apps, Airflow components), and work waiting for a slot. */
  in_use?: number | null; queued?: number | null;
}

export interface WorkspaceSettings {
  warehouse: string | null;
  max_running: number | null;
  max_running_per_user: number;
  running?: number; queued?: number;
}

export interface WarehouseOccupant {
  category: "job" | "app" | "airflow" | "trino" | "workspace";
  kind: string;
  state: "running" | "queued";
  slots: number;
  name: string;
  owner: string;
  layer_id: string | null; layer_name: string | null;
  queue_position: number | null;
  run_id: string | null; workload_id: string | null; workspace_id: string | null;
}
export interface WarehouseOverview {
  name: string; cpu: number; memory_gb: number; gpu: number;
  max_concurrent: number; in_use: number; queued: number;
  dedicated: boolean; allowed_groups: string[];
  layer_ids: string[]; layer_names: string[]; workload_kinds: string[];
  layers: { layer_id: string | null; layer_name: string | null; in_use: number; queued: number }[];
  kinds: { kind: string; in_use: number; queued: number }[];
  /** Work you may see; the rest is counted in others_*. */
  occupants: WarehouseOccupant[];
  others_in_use: number; others_queued: number;
  workspaces: { running: number; queued: number; max_running: number | null } | null;
}

/** Warehouses work can be placed on: all but those that run Trino workers. */
/** Warehouses work may choose: not Trino's, and meant for its layer and kind when those are known. */
export const usablePools = (pools?: Pool[], layerId?: string | null, kind?: string | null) => (pools ?? []).filter((p) =>
  !p.trino_layers?.length
  && (!layerId || !p.layer_ids?.length || p.layer_ids.includes(layerId))
  && (!kind || !p.workload_kinds?.length || p.workload_kinds.includes(kind as WorkloadKind)));
/** The warehouse a choice starts with: the declared one if it exists and is meant for the work, else the most
 *  specific default (layer and kind, the layer, the kind), else the installation default. */
export function startingPool(pools: Pool[] | undefined, declared?: string | null, layerId?: string | null, kind?: string | null): string {
  const usable = usablePools(pools, layerId, kind);
  if (declared && usable.some((p) => p.name === declared)) return declared;
  const scoped = (l: string | null, k: string | null) => usable.find((p) => p.default_for?.some((d) => d.layer_id === l && d.workload_kind === k));
  const match = (layerId && kind ? scoped(layerId, kind) : undefined) ?? (layerId ? scoped(layerId, null) : undefined)
    ?? (kind ? scoped(null, kind) : undefined);
  return match?.name ?? usable.find((p) => p.default)?.name ?? "";
}

/** "2nd in line" for queued work. */
export function inLine(position?: number | null): string {
  if (!position) return "waiting for capacity";
  const suffix = position % 100 >= 11 && position % 100 <= 13 ? "th" : ({ 1: "st", 2: "nd", 3: "rd" } as Record<number, string>)[position % 10] ?? "th";
  return `${position}${suffix} in line`;
}

export interface Me {
  principal_id?: string;
  active_group_id?: string | null;
  available_groups?: { id: string; name: string; system?: boolean }[];
  username: string;
  groups: string[];
  is_admin: boolean;
  data_sources: DataSource[];
  pools: Pool[];
  /** Optional parts of the installation, e.g. { r: false } when R is left out. */
  features?: Record<string, boolean>;
}

/** R (RStudio, R and Shiny programs, R packages) is optional per installation. */
export const rEnabled = (me?: Me | null) => me?.features?.r !== false;

export type Editor = "vscode" | "rstudio" | "sql";
export const EDITOR_NAME: Record<Editor, string> = { vscode: "VS Code", rstudio: "RStudio", sql: "SQL" };
export const EDITOR_LABEL: Record<Editor, string> = {
  vscode: "VS Code · Python, Jupyter, Streamlit", rstudio: "RStudio · R, Shiny", sql: "SQL · CloudBeaver, query any data you can access" };

export interface Workspace {
  owner_group_id?: string | null; owner_group_name?: string | null;
  layer_id?: string | null; layer_name?: string | null;
  environment_id?: string | null;
  id: string;
  owner: string;
  name: string;
  editor: Editor;
  status: "queued" | "starting" | "running" | "stopped" | "failed";
  queue_position?: number | null;
  /** Why the editor has not started yet (no room in the cluster, image cannot be pulled). */
  waiting_reason?: string | null;
  /** Created for this person from a group template. */
  template_id?: string | null;
  /** Which of the owner's Git credentials it uses; git_asked: the owner chose one (or none). */
  git_credential_id?: string | null;
  git_asked?: boolean;
  started_at?: string | null;
  startup_error?: string | null;
  last_activity_at: string;
  created_at: string;
}

export type RunKind = "script" | "notebook" | "app" | "dbt" | "airflow";

export interface Run {
  initiated_by: string | null;
  id: string;
  owner: string;
  workspace_id: string | null;
  workload_id: string | null;
  commit_sha: string | null;
  kind: RunKind;
  entrypoint: string;
  program: string | null;
  args: Record<string, unknown>;
  runtime: "python" | "r" | "dbt" | "airflow" | null;
  runtime_version: string | null;
  status: "queued" | "pending" | "running" | "succeeded" | "failed" | "stopped";
  queue_position?: number | null;
  /** Why it has not started yet (no room in the cluster, image cannot be pulled). */
  waiting_reason?: string | null;
  pool_name: string;
  created_at: string;
  finished_at: string | null;
}

/** Runs of deployed programs, or runs started in workspaces (private to the workspace's owner and admins). */
export type RunScope = "deployed" | "workspace";

export const isActive = (r: Run) => r.status === "queued" || r.status === "pending" || r.status === "running";

export type ArgType = "string" | "text" | "integer" | "number" | "boolean" | "choice" | "date" | "data_source";

export interface ArgDef {
  name: string;
  type: ArgType;
  label?: string | null;
  help?: string | null;
  required?: boolean;
  default?: string | number | boolean | null;
  choices?: string[] | null;
  min?: number | null;
  max?: number | null;
}

export interface ProgramDef {
  description: string;
  layer?: string | null;
  kind: RunKind;
  entrypoint: string;
  runtime: "python" | "r";
  python?: string | null;
  r?: string | null;
  dbt_version?: string | null;
  airflow?: string | null;
  dbt?: { data_source: string; schema: string; dev_schema: string } | null;
  secrets?: Record<string, string>;
  requirements?: string | null;
  warehouse?: string | null;
  data_sources: string[];
  args: ArgDef[];
}

export interface WorkspaceConfig {
  exists: boolean;
  live: boolean;
  read_at: string | null;
  errors: string[];
  config: { warehouse?: string | null; programs: Record<string, ProgramDef> } | null;
}

export interface OutputFile {
  path: string;
  size: number;
}

/** A group's default workspace: each member gets their own private, stopped workspace from it. */
export interface WorkspaceTemplate {
  id: string; name: string; group_id: string; group_name?: string | null;
  editor: Editor; layer_id: string; layer_name?: string | null; description: string;
  repositories: string[]; workspaces?: number;
}
export type WorkspaceTemplateInput = Omit<WorkspaceTemplate, "id" | "group_name" | "layer_name" | "workspaces">;

export interface QueryLogRow {
  id: string; layer_id: string; query_id: string; actor: string; principal_id: string; run_id: string; workload_id: string;
  workspace_id: string; trino_user: string; source: string; query_type: string; state: string; error: string; sql_text: string | null;
  tables: string[]; output_table: string; rows: number; bytes: number; cpu_ms: number; wall_ms: number; queued_ms: number;
  created_at: string; ended_at: string | null;
}
export interface QueryLogSettings { retention_days: number; record_sql: boolean }

export type PackageKind = "python" | "r";
export interface PackageSummary {
  id: string; kind: PackageKind; name: string; key: string; origin: "internal" | "pypi" | "cran"; summary: string;
  latest: string | null; versions: number; owner_group: string | null; resource_id: string | null; created_by: string; created_at: string;
}
export interface PackageFileInfo { filename: string; size: number; sha256: string; requires_python: string | null; yanked: boolean; yanked_reason: string; uploaded_by: string; created_at: string }
export interface PackageVersion { version: string; yanked: boolean; requires: string[]; install: string; files: PackageFileInfo[] }
export interface PackageDetail extends Omit<PackageSummary, "versions"> { versions: PackageVersion[]; version_count: number; actions: string[]; install: string }
export interface PackageList { packages: PackageSummary[]; actions: string[]; resource_id: string; upstream: { python: boolean; r: boolean }; kinds?: PackageKind[]; max_upload_mb: number }
export interface PackageImport { id: string; kind: PackageKind; requirement: string; status: "queued" | "running" | "done" | "failed"; requested_by: string; log: string; added: string[]; created_at: string; finished_at: string | null }
export interface AllowedPackage { name: string; description: string }
export interface PackageAllowlist { enabled: boolean; python: AllowedPackage[]; r: AllowedPackage[] }
export const PACKAGE_KIND_LABEL: Record<PackageKind, string> = { python: "Python", r: "R" };
export const PACKAGE_ORIGIN_LABEL: Record<PackageSummary["origin"], string> = { internal: "Published here", pypi: "PyPI", cran: "CRAN" };

/** One of your own Git tokens (the token itself is never returned). */
export interface GitCredential { id: string; label: string; host: string; username: string; commit_name: string; commit_email: string; updated_at: string }
export interface GitCredentialInput { label: string; host: string; username: string; token?: string; commit_name: string; commit_email: string }
/** VS Code and RStudio use Git; the SQL editor does not. */
export const usesGit = (ws: Pick<Workspace, "editor">) => ws.editor === "vscode" || ws.editor === "rstudio";

/** Host names VS Code and RStudio may reach over HTTPS through the egress proxy; proxy: the installation runs it. */
export interface EgressHost { host: string; description: string }
export interface EgressAllowlist { hosts: EgressHost[]; proxy: boolean }

export interface Repo {
  id: string;
  name: string;
  url: string;
  default_branch: string;
  description: string;
  allowed_groups: string[];
  has_credentials: boolean;
}

export interface Commit {
  sha: string;
  subject: string;
  author: string;
  date: string;
}

export interface PullResult {
  commit: Commit;
  programs: Record<string, ProgramDef>;
  errors: string[];
}

export type RuntimeKind = "python" | "r" | "dbt" | "airflow";

export interface Runtime {
  id: string;
  kind: RuntimeKind;
  version: string;
  image: string;
  description: string;
  is_default: boolean;
  enabled: boolean;
  /** Shipped with the installation: its image comes from the Helm values and follows them on upgrades. */
  installation?: boolean;
}

/** Public facts about a certificate secret; key material is never returned. */
export interface CertificateInfo {
  subject: string; issuer: string; serial: string; not_before: string; not_after: string;
  sha256_fingerprint: string; subject_alt_names: string[]; has_private_key: boolean; chain_length: number;
}

export interface Secret {
  scope: "user" | "group" | "sa";
  owner: string;
  name: string;
  kind: "string" | "certificate";
  ref: string;
  description: string;
  updated_by: string;
  updated_at: string;
  certificate: CertificateInfo | null;
}

/** Write-only secret material: a string value, or a certificate (PEM, or PKCS#12 as base64). */
export type SecretInput =
  | { kind: "string"; value: string; description?: string }
  | { kind: "certificate"; certificate?: string; private_key?: string; ca_chain?: string; pkcs12_base64?: string; password?: string; description?: string };

export interface ServiceAccount {
  layer_id?: string | null; layer_name?: string | null;
  environment_id?: string;
  can_manage: boolean; resource_id: string;
  id: string;
  name: string;
  principal: string;
  description: string;
  groups: string[];
  allowed_groups: string[];
  disabled: boolean;
  created_by: string;
  created_at: string;
}

export interface ServiceToken {
  id: string;
  name: string;
  prefix: string;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  revoked: boolean;
  stored_as: string | null;
  token: string | null;
}

/** "Python 3.12", "dbt 1.8": what a program or run uses. */
export function runtimeLabel(kind?: string | null, version?: string | null): string {
  const name = ({ python: "Python", r: "R", dbt: "dbt", airflow: "Airflow" } as Record<string, string>)[kind ?? ""] ?? kind ?? "";
  return version ? `${name} ${version}` : name;
}

export function programRuntime(p: ProgramDef): { kind: RuntimeKind; version: string | null } {
  if (p.kind === "dbt") return { kind: "dbt", version: p.dbt_version ?? null };
  if (p.kind === "airflow") return { kind: "airflow", version: p.airflow ?? null };
  return p.runtime === "r" ? { kind: "r", version: p.r ?? null } : { kind: "python", version: p.python ?? null };
}

export interface WorkloadEvent {
  at: string;
  kind: string;
  sha: string | null;
  message: string;
}

export interface Workload {
  owner_group_id?: string | null; owner_group_name?: string | null;
  layer_id?: string | null; layer_name?: string | null;
  execution_state?: string;
  resource_id: string; folder_id: string; service_principal_id: string;
  execution_mode: "caller" | "fixed"; actions: string[];
  id: string;
  slug: string;
  title: string;
  kind: RunKind;
  runtime: "python" | "r";
  program: string;
  repo: string;
  branch: string;
  owner: string;
  allowed_groups: string[];
  warehouse: string;
  auto_update: boolean;
  status: "queued" | "live" | "deploying" | "failed" | "stopped";
  /** Set while a deploy waits for warehouse capacity; the running version (if any) keeps serving. */
  queued_sha?: string | null;
  queue_position?: number | null;
  deployed_sha: string | null;
  deployed_commit: { subject?: string; author?: string; date?: string };
  deployed_at: string | null;
  latest_sha: string | null;
  last_checked_at: string | null;
  sync_error: string | null;
  access_issue?: DataAccessIssue | null;
  program_def: ProgramDef;
  runtime_version: string | null;
  run_as: string | null;
  identity: string;
  env: Record<string, string>;
  secrets: Record<string, string>;
  url: string | null;
  can_manage: boolean;
  created_at: string;
  events?: WorkloadEvent[];
}

export interface OutputContent extends OutputFile {
  truncated: boolean;
  content_base64: string;
}

export function workloadStatus(p: Workload): string {
  if (p.status === "queued") return `Waiting for capacity · ${inLine(p.queue_position)}`;
  if (p.status !== "live") return p.status === "deploying" ? "Deploying…" : p.status === "stopped" ? "Undeployed" : "Failed";
  if (p.kind === "script" || p.kind === "notebook") return "Ready to run";
  if (p.kind === "dbt") return "Docs available · runs on demand";
  return p.execution_state === "running" ? "Running" : p.execution_state === "pending" ? "Starting…"
    : p.execution_state === "unknown" ? "Status unavailable" : p.execution_state ? "Not running" : "Deployed";
}
export function workloadDot(p: Workload): string {
  return p.status === "queued" ? "queued" : p.status === "deploying" || p.execution_state === "pending" ? "pending" : p.status === "failed" ? "failed"
    : p.status === "live" && (p.kind === "script" || p.kind === "notebook" || p.kind === "dbt") ? "ready"
    : p.execution_state === "running" ? "running" : "stopped";
}

export interface AuditEvent {
  at: string;
  actor: string;
  action: string;
  target: string;
  detail: Record<string, unknown>;
}

export interface DataAccessIssue {
  code: "data_access_denied";
  message: string;
  guidance: string;
  identity: { name: string; principal_id: string | null; kind?: "service" | "user" };
  can_manage_groups: boolean;
  sources: { name: string; resource_id: string | null; can_assign: boolean;
    schemas?: { id: string; name: string; can_assign: boolean }[] }[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public fieldErrors: Record<string, string> = {}, public accessIssue?: DataAccessIssue,
    public slaIssue?: {message:string;assets:{asset_id:string;name?:string;status:string}[]}) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const send = async (force: boolean) => fetch(`${getServer()}/api${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${await accessToken(force)}`,
      ...(body !== undefined && !(body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
    },
    body: body instanceof FormData ? body : body !== undefined ? JSON.stringify(body) : undefined,
  });
  let res = await send(false);
  // The token can still expire in flight, or the clock can be off: renew once and retry.
  if (res.status === 401) res = await send(true);
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    // Validation errors come as {message, errors}: per-field for arguments, a list for fdp.json.
    if (detail && typeof detail === "object" && "code" in detail && detail.code === "data_access_denied") {
      const issue = detail as DataAccessIssue;
      throw new ApiError(res.status, issue.message, {}, issue);
    }
    if(detail&&typeof detail==='object'&&'code' in detail&&detail.code==='SLA_INPUT_GATE') {
      const issue=detail as {code:string;message:string;assets:{asset_id:string;name?:string;status:string}[]};
      throw new ApiError(res.status,issue.message,{},undefined,issue);
    }
    if (detail && typeof detail === "object" && "errors" in detail) {
      const d = detail as { message?: string; errors: Record<string, string> | string[] };
      if (Array.isArray(d.errors)) throw new ApiError(res.status, `${d.message}: ${d.errors.join("; ")}`);
      throw new ApiError(res.status, d.message ?? "invalid request", d.errors);
    }
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return undefined as T;
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("json") ? res.json() : res.text()) as Promise<T>;
}

export interface CatalogGovernance {
  description?: string; domain?: string; owner_group_id?: string | null; steward?: string;
  classification?: string; status?: string; tags?: string[]; lineage_role?: "data" | "operational";
}
export interface CatalogSLA { freshness_minutes?: number | null; success_rate_percent?: number | null; window_hours?: number;
  warning_percent?: number; freshness_basis?: "refresh" | "checked" | "watermark"; deadline_time?: string | null; timezone?: string;
  weekdays?: number[]; grace_minutes?: number; warning_minutes?: number; quality_required?: boolean; quality_max_age_minutes?: number;
  required_assets?: string[]; enforcement?: "monitor" | "block_new_runs"; block_on_unknown?: boolean }
export interface MetricObservation { id: string; observed_at: string; value: number | null; status: string; definition_version: number; origin: string; run_id: string; revision: string; detail: Record<string, unknown> }
export interface MetricDefinition { id: string; asset_id: string; key: string; name: string; kind: "kpi" | "quality"; description: string; unit: string; origin: string; enabled: boolean; version: number;
  definition: { source?: string; sql?: string; dependencies?: string[]; minimum?: number | null; maximum?: number | null; warn_minimum?: number | null; warn_maximum?: number | null }; latest: MetricObservation | null }
export interface SlaIncident { id: string; asset_id: string; asset_name?:string; status: string; opened_at: string; updated_at: string; resolved_at: string | null; acknowledged_at: string | null; detail: {checks?: {name: string; status: string}[]} }
export interface CatalogAsset {
  layer_id?: string | null; layer_name?: string | null;
  id: string; name: string; kind: string; path: string; resource: AccessResource;
  attributes: { description?: string; origin?: string; repo?: string; branch?: string; program?: string; runtime?: string; source_kind?: string;
    columns?: { name: string; data_type?: string; description?: string }[]; object_path?: string; test_status?: string; [key: string]: unknown };
  governance: CatalogGovernance; sla: CatalogSLA;
  owner_group_name?: string | null;
  score?: number; matched_fields?: string[]; approximate?: boolean;
  service_level: { status: string; checks: { name: string; status: string; target: number | string; value: number | null; unit: string; samples?: number; compliance_percent?: number | null; next_deadline_at?: string | null }[]; last_success_at?: string | null };
  observed_at: string; can_edit: boolean; can_govern: boolean;
  imports?: { origin: string; scope: string; revision: string; nodes: number; edges: number; observed_at: string }[];
}
export interface CatalogSuggestion { id: string; name: string; kind: string; path: string; approximate?: boolean }
export interface CatalogSearch { items: CatalogAsset[]; total: number; offset: number; limit: number; approximate?: boolean; facets: { kind: string[]; domain: string[]; branch: string[]; layer?: string[]; tag?: string[]; owner?: string[]; platform?: string[] } }
export interface CatalogGraph { nodes: (CatalogAsset & { distance: number; lineage_parent_id?: string; lineage_child_count?: number; upstream_count?: number; downstream_count?: number })[]; edges: { id: string; source: string; target: string; relation: string; origin: string; revision: string; observed_at: string; control?: boolean; category?: "data" | "operational" | "schema"; collapsed_count?: number; connections?: { id: string; source: string; target: string; source_name: string; target_name: string; relation: string; origin: string; revision: string; observed_at: string }[]; evidence?: { id: string; origin: string; revision: string; observed_at: string }[] }[]; truncated: boolean }

export interface OrchestratorRun { dag_id: string; run_id: string; state: string; logical_date: string | null; start_date: string | null; end_date: string | null }
export interface OrchestratorHistory { runs: OrchestratorRun[]; dags: string[] }

export const api = {
  warehouses: () => request<WarehouseOverview[]>("GET", "/v1/compute/warehouses"),
  selectActiveGroup: (group_id: string) => request<{active_group_id: string; name: string}>("PUT","/v1/me/active-group",{group_id}),
  layers: {
    list: () => request<Layer[]>("GET", "/v1/layers"),
    create: (body: Omit<Layer, "id" | "default">) => request<Layer>("POST", "/v1/admin/layers", body),
    update: (id: string, body: Omit<Layer, "id" | "default">) => request<Layer>("PUT", `/v1/admin/layers/${id}`, body),
    remove: (id: string) => request<void>("DELETE", `/v1/admin/layers/${id}`),
    setDefault: (layer_id: string) => request<{ layer_id: string; configured: boolean }>("PUT", "/v1/admin/settings/default-layer", { layer_id }),
  },
  orchestratorRuns: (id: string, dagId = "") => request<OrchestratorHistory>("GET", `/v1/workloads/${encodeURIComponent(id)}/orchestrator-runs?${new URLSearchParams({ limit: "3", dag_id: dagId })}`),
  catalog: {
    resourceAsset: (resourceId: string, objectPath?: string) => request<CatalogAsset>("GET", `/v1/catalog/resources/${resourceId}/asset${objectPath === undefined ? "" : "?" + new URLSearchParams({ object_path: objectPath })}`),
    metrics: (id: string) => request<MetricDefinition[]>("GET", `/v1/catalog/assets/${id}/metrics`),
    defineMetric: (id: string, body: unknown) => request<MetricDefinition>("POST", `/v1/catalog/assets/${id}/metrics`, body),
    metricHistory: (id: string, metric: string) => request<MetricObservation[]>("GET", `/v1/catalog/assets/${id}/metrics/${metric}/history`),
    measureMetric: (id: string, metric: string) => request<MetricObservation>("POST", `/v1/catalog/assets/${id}/metrics/${metric}/measure`),
    reportMetric: (id: string, metric: string, value: number) => request<MetricObservation>("POST", `/v1/catalog/assets/${id}/metrics/${metric}/observations`, {value}),
    slaHistory: (id: string) => request<{id: string; evaluated_at: string; status: string}[]>("GET", `/v1/catalog/assets/${id}/sla-history`),
    evaluateSla: (id: string) => request<unknown>("POST", `/v1/catalog/assets/${id}/evaluate-sla`),
    alerts: (id = "") => request<SlaIncident[]>("GET", `/v1/catalog/alerts?${new URLSearchParams({asset_id:id})}`),
    acknowledgeAlert: (id: string) => request<unknown>("POST", `/v1/catalog/alerts/${id}/acknowledge`),
    suggest: (q: string) => request<{ items: CatalogSuggestion[] }>("GET", `/v1/catalog/suggest?${new URLSearchParams({ q })}`),
    search: (filters: Record<string, string> = {}) => request<CatalogSearch>("GET", `/v1/catalog/search?${new URLSearchParams(filters)}`),
    asset: (id: string) => request<CatalogAsset>("GET", `/v1/catalog/assets/${id}`),
    governance: (id: string, body: CatalogGovernance) => request<CatalogAsset>("PUT", `/v1/catalog/assets/${id}/governance`, body),
    sla: (id: string, body: CatalogSLA) => request<CatalogAsset>("PUT", `/v1/catalog/assets/${id}/sla`, body),
    lineage: (id: string, filters: Record<string, string> = {}) => request<CatalogGraph>("GET", `/v1/catalog/assets/${id}/lineage?${new URLSearchParams(filters)}`),
    import: (id: string, kind: "dbt" | "airflow", body: unknown) => request<{ ok: boolean; asset_id: string }>("POST", `/v1/catalog/resources/${id}/import/${kind}`, body),
  },
  dataPreview: (schemaId: string, table: string) => request<{ columns: string[]; rows: unknown[][]; limit: number; query_id: string }>(
    "GET", `/v1/data/schemas/${encodeURIComponent(schemaId)}/preview?${new URLSearchParams({ table })}`),
  metadataQuery: (sql: string) => request<{ columns: string[]; rows: unknown[][]; rowcount: number; truncated: boolean; query_id: string }>(
    "POST", "/v1/sql/query", { source: "fdp-metadata", sql, max_rows: 1000 }),
  dataChildren: (schemaId: string, options: { table?: string; path?: string; after?: string } = {}) => {
    const params = new URLSearchParams(options);
    return request<DataChildren>("GET", `/v1/data/schemas/${encodeURIComponent(schemaId)}/children?${params}`);
  },
  access: {
    groupAssignments: (id: string) => request<{ resource: AccessResource; role: string }[]>("GET", `/v1/rbac/groups/${id}/assignments`),
    roles: () => request<Record<string, Record<string, string[]>>>("GET", "/v1/rbac/roles"),
    directory: () => request<DirectoryPrincipal[]>("GET", "/v1/rbac/directory"),
    groups: () => request<AppGroup[]>("GET", "/v1/rbac/groups"),
    createGroup: (body: { name: string; description: string; owners: string[] }) => request<AppGroup>("POST", "/v1/rbac/groups", body),
    updateGroup: (id: string, body: { name: string; description: string; owners: string[] }) => request<AppGroup>("PUT", `/v1/rbac/groups/${id}`, body),
    deleteGroup: (id: string) => request<void>("DELETE", `/v1/rbac/groups/${id}`),
    bind: (id: string, principal_id: string) => request<AppGroup>("POST", `/v1/rbac/groups/${id}/bindings`, { principal_id }),
    unbind: (id: string, pid: string) => request<void>("DELETE", `/v1/rbac/groups/${id}/bindings/${pid}`),
    resources: () => request<AccessResource[]>("GET", "/v1/rbac/resources"),
    resource: (id: string) => request<AccessResource>("GET", `/v1/rbac/resources/${id}`),
    folder: (name: string, parent_id: string, layer_id?: string) => request<AccessResource>("POST", "/v1/rbac/folders", { name, parent_id, ...(layer_id ? { layer_id } : {}) }),
    move: (id: string, name: string, parent_id: string) => request<AccessResource>("PATCH", `/v1/rbac/resources/${id}`, { name, parent_id }),
    deleteFolder: (id: string) => request<void>("DELETE", `/v1/rbac/folders/${id}`),
    schema: (source: string, name: string, prefix?: string) => request<AccessResource>("POST", `/v1/rbac/sources/${encodeURIComponent(source)}/schemas`, { name, ...(prefix !== undefined ? { prefix } : {}) }),
    provision: (id: string) => request<AccessResource>("POST", `/v1/rbac/resources/${id}/provision`),
    assignments: (id: string) => request<RoleAssignment[]>("GET", `/v1/rbac/resources/${id}/assignments`),
    userPrincipals: (id: string) => request<ServicePrincipal[]>("GET", `/v1/rbac/resources/${id}/user-principals`),
    servicePrincipals: (id: string) => request<ServicePrincipal[]>("GET", `/v1/rbac/resources/${id}/service-principals`),
    assign: (id: string, app_group_id: string, role: string) => request<RoleAssignment[]>("PUT", `/v1/rbac/resources/${id}/assignments`, { app_group_id, role }),
    assignService: (id: string, principal_id: string, role: string) => request<RoleAssignment[]>("PUT", `/v1/rbac/resources/${id}/assignments`, { principal_id, role }),
    revoke: (id: string, gid: string) => request<void>("DELETE", `/v1/rbac/resources/${id}/assignments/${gid}`),
    revokeService: (id: string, pid: string) => request<void>("DELETE", `/v1/rbac/resources/${id}/assignments/services/${pid}`),
  },
  dataPolicies: {
    list: (schemaId: string) => request<DataPolicy[]>("GET", `/v1/data-policies?schema_id=${encodeURIComponent(schemaId)}`),
    create: (body: DataPolicyChange & Pick<DataPolicy, "schema_id" | "table" | "kind" | "column">) => request<DataPolicy>("POST", "/v1/data-policies", body),
    update: (id: string, body: DataPolicyChange) => request<DataPolicy>("PUT", `/v1/data-policies/${id}`, body),
    remove: (id: string) => request<void>("DELETE", `/v1/data-policies/${id}`),
  },
  me: () => request<Me>("GET", "/v1/me"),

  workspaces: () => request<Workspace[]>("GET", "/v1/workspaces"),
  createWorkspace: (name: string, editor: Editor, layer_id?: string) => request<Workspace>("POST", "/v1/workspaces", { name, editor, layer_id }),
  startWorkspace: (id: string) => request<Workspace>("POST", `/v1/workspaces/${id}/start`),
  setWorkspaceGit: (id: string, credential_id: string | null) => request<Workspace>("PUT", `/v1/workspaces/${id}/git`, { credential_id }),
  stopWorkspace: (id: string) => request<Workspace>("POST", `/v1/workspaces/${id}/stop`),
  deleteWorkspace: (id: string) => request<void>("DELETE", `/v1/workspaces/${id}`),
  openWorkspace: (id: string) => request<{ url: string }>("POST", `/v1/workspaces/${id}/open`),

  workspaceConfig: (id: string) => request<WorkspaceConfig>("GET", `/v1/workspaces/${id}/config`),

  runs: (limit = 100, sort: "created" | "updated" = "created") => request<Run[]>("GET", `/v1/runs?limit=${limit}${sort === "updated" ? "&sort=updated" : ""}`),
  createRun: (workspace_id: string, pool: string, kind: RunKind, entrypoint: string) =>
    request<Run>("POST", "/v1/runs", { workspace_id, pool, kind, entrypoint }),
  runProgram: (workspace_id: string, program: string, args: Record<string, unknown>, pool?: string) =>
    request<Run>("POST", "/v1/runs", { workspace_id, program, args, ...(pool ? { pool } : {}) }),
  stopRun: (id: string) => request<Run>("POST", `/v1/runs/${id}/stop`),
  runLogs: (id: string) => request<string>("GET", `/v1/runs/${id}/logs?tail=20000`),
  runOutputs: (id: string) => request<OutputFile[]>("GET", `/v1/runs/${id}/outputs`),
  runOutput: (id: string, path: string) =>
    request<OutputContent>("GET", `/v1/runs/${id}/outputs/${path.split("/").map(encodeURIComponent).join("/")}`),
  openApp: (id: string) => request<{ url: string }>("POST", `/v1/runs/${id}/open`),

  repos: () => request<Repo[]>("GET", "/v1/repos"),
  branches: (repoId: string) => request<{ name: string; sha: string }[]>("GET", `/v1/repos/${repoId}/branches`),
  pull: (repoId: string, branch: string) =>
    request<PullResult>("POST", `/v1/repos/${repoId}/pull?branch=${encodeURIComponent(branch)}`),
  workloads: () => request<Workload[]>("GET", "/v1/workloads"),
  workload: (id: string) => request<Workload>("GET", `/v1/workloads/${id}`),
  deploy: (body: { repo_id: string; branch: string; program: string; slug: string; title: string;
                    layer_id?: string; warehouse?: string; allowed_groups: string[]; auto_update: boolean; run_as?: string;
                    env: Record<string, string>; secrets: Record<string, string>; folder_id: string; execution_mode: "caller" | "fixed" }) =>
    request<Workload>("POST", "/v1/workloads", body),
  updateWorkload: (id: string, patch: Partial<Pick<Workload, "title" | "warehouse" | "allowed_groups" | "auto_update" | "env" | "secrets">> & { run_as?: string; execution_mode?: "caller" | "fixed" }) =>
    request<Workload>("PATCH", `/v1/workloads/${id}`, patch),
  runtimes: () => request<Record<RuntimeKind, Runtime[]>>("GET", "/v1/runtimes"),
  secrets: () => request<Secret[]>("GET", "/v1/secrets"),
  apis: {
    list: () => request<{ apis: ApiDefinition[]; can_create: boolean }>("GET", "/v1/apis"),
    get: (id: string) => request<ApiDefinition>("GET", `/v1/apis/${id}`),
    create: (body: ApiInput) => request<ApiDefinition>("POST", "/v1/apis", body),
    update: (id: string, body: ApiInput) => request<ApiDefinition>("PUT", `/v1/apis/${id}`, body),
    remove: (id: string) => request<void>("DELETE", `/v1/apis/${id}`),
    createOperation: (id: string, body: OperationInput) => request<ApiOperation>("POST", `/v1/apis/${id}/operations`, body),
    updateOperation: (id: string, op: string, body: OperationInput) => request<ApiOperation>("PUT", `/v1/apis/${id}/operations/${op}`, body),
    removeOperation: (id: string, op: string) => request<void>("DELETE", `/v1/apis/${id}/operations/${op}`),
    calls: (id: string) => request<ApiCallRecord[]>("GET", `/v1/apis/${id}/calls`),
  },
  setSecret: (scope: string, owner: string, name: string, body: SecretInput) =>
    request<Secret>("PUT", `/v1/secrets/${scope}/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`, body),
  deleteSecret: (scope: string, owner: string, name: string) =>
    request<void>("DELETE", `/v1/secrets/${scope}/${encodeURIComponent(owner)}/${encodeURIComponent(name)}`),
  serviceAccounts: () => request<ServiceAccount[]>("GET", "/v1/service-accounts"),
  tokens: (sa: string) => request<ServiceToken[]>("GET", `/v1/service-accounts/${sa}/tokens`),
  createToken: (sa: string, body: { name: string; expires_days?: number; store_as_secret?: string }) =>
    request<ServiceToken>("POST", `/v1/service-accounts/${sa}/tokens`, body),
  revokeToken: (sa: string, id: string) => request<void>("DELETE", `/v1/service-accounts/${sa}/tokens/${id}`),
  syncWorkload: (id: string) => request<Workload>("POST", `/v1/workloads/${id}/sync`),
  undeploy: (id: string) => request<void>("DELETE", `/v1/workloads/${id}`),
  openWorkload: (id: string) => request<{ url: string }>("POST", `/v1/workloads/${id}/open`),
  runDeployed: (id: string, args: Record<string, unknown>, pool?: string) =>
    request<Run>("POST", `/v1/workloads/${id}/runs`, { args, ...(pool ? { pool } : {}) }),

  queryLog: (filters: { actor?: string; layer_id?: string; state?: string; table?: string; before?: string } = {}) =>
    request<QueryLogRow[]>("GET", `/v1/query-log?${new URLSearchParams(Object.entries(filters).filter(([, v]) => v) as [string, string][])}`),
  queryLogSettings: () => request<QueryLogSettings>("GET", "/v1/admin/settings/query-log"),
  saveQueryLogSettings: (s: QueryLogSettings) => request<QueryLogSettings>("PUT", "/v1/admin/settings/query-log", s),
  packages: {
    list: (kind?: PackageKind) => request<PackageList>("GET", `/v1/packages${kind ? `?kind=${kind}` : ""}`),
    get: (kind: PackageKind, name: string) => request<PackageDetail>("GET", `/v1/packages/${kind}/${encodeURIComponent(name)}`),
    upload: (kind: PackageKind, file: File) => { const form = new FormData(); form.append("file", file); return request<{ package: PackageSummary; created: boolean }>("POST", `/v1/packages/${kind}/upload`, form); },
    importPackage: (kind: PackageKind, requirement: string) => request<PackageImport>("POST", "/v1/packages/imports", { kind, requirement }),
    imports: () => request<PackageImport[]>("GET", "/v1/packages/imports"),
    allowlist: () => request<PackageAllowlist>("GET", "/v1/packages/allowlist"),
    saveAllowlist: (a: PackageAllowlist) => request<PackageAllowlist>("PUT", "/v1/packages/allowlist", a),
    yank: (kind: PackageKind, name: string, version: string, reason: string) =>
      request<PackageDetail>("POST", `/v1/packages/${kind}/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/yank`, { reason }),
    unyank: (kind: PackageKind, name: string, version: string) =>
      request<PackageDetail>("DELETE", `/v1/packages/${kind}/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/yank`),
    deleteVersion: (kind: PackageKind, name: string, version: string) =>
      request<void>("DELETE", `/v1/packages/${kind}/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}`),
  },
  gitCredentials: {
    list: () => request<GitCredential[]>("GET", "/v1/me/git-credentials"),
    create: (c: GitCredentialInput) => request<GitCredential>("POST", "/v1/me/git-credentials", c),
    update: (id: string, c: GitCredentialInput) => request<GitCredential>("PUT", `/v1/me/git-credentials/${id}`, c),
    remove: (id: string) => request<void>("DELETE", `/v1/me/git-credentials/${id}`),
  },

  admin: {
    runtimes: () => request<Runtime[]>("GET", "/v1/admin/runtimes"),
    upsertRuntime: (kind: string, version: string, body: { image: string; description: string; is_default: boolean; enabled: boolean }) =>
      request<Runtime>("PUT", `/v1/admin/runtimes/${kind}/${encodeURIComponent(version)}`, body),
    deleteRuntime: (kind: string, version: string) => request<void>("DELETE", `/v1/admin/runtimes/${kind}/${encodeURIComponent(version)}`),
    upsertServiceAccount: (name: string, body: { layer_id?: string | null; environment_id?: string; description: string; groups: string[]; allowed_groups: string[]; disabled: boolean }) =>
      request<ServiceAccount>("PUT", `/v1/admin/service-accounts/${name}`, body),
    deleteServiceAccount: (name: string) => request<void>("DELETE", `/v1/admin/service-accounts/${name}`),
    repos: () => request<Repo[]>("GET", "/v1/admin/repos"),
    workspaceTemplates: () => request<WorkspaceTemplate[]>("GET", "/v1/admin/workspace-templates"),
    createWorkspaceTemplate: (t: WorkspaceTemplateInput) => request<WorkspaceTemplate>("POST", "/v1/admin/workspace-templates", t),
    updateWorkspaceTemplate: (id: string, t: WorkspaceTemplateInput) => request<WorkspaceTemplate>("PUT", `/v1/admin/workspace-templates/${id}`, t),
    deleteWorkspaceTemplate: (id: string) => request<void>("DELETE", `/v1/admin/workspace-templates/${id}`),
    upsertRepo: (r: Omit<Repo, "id" | "has_credentials"> & { secret?: Record<string, string> }) =>
      request<Repo>("PUT", `/v1/admin/repos/${r.name}`, r),
    deleteRepo: (name: string) => request<void>("DELETE", `/v1/admin/repos/${name}`),
    sources: () => request<DataSource[]>("GET", "/v1/admin/datasources"),
    storageTypes: () => request<StorageType[]>("GET", "/v1/admin/storage-types"),
    upsertSource: (ds: Omit<DataSource, "id" | "groups"> & { secret?: Record<string, string>; provisioner_secret?: Record<string, string> }) =>
      request<DataSource>("PUT", `/v1/admin/datasources/${ds.name}`, ds),
    deleteSource: (name: string) => request<void>("DELETE", `/v1/admin/datasources/${name}`),
    grant: (name: string, group: string) => request<DataSource>("POST", `/v1/admin/datasources/${name}/grants`, { group }),
    revoke: (name: string, group: string) =>
      request<DataSource>("DELETE", `/v1/admin/datasources/${name}/grants/${encodeURIComponent(group)}`),
    pools: () => request<Pool[]>("GET", "/v1/admin/pools"),
    upsertPool: (p: Omit<Pool, "id">) => request<Pool>("PUT", `/v1/admin/pools/${p.name}`, p),
    setDefaultWarehouse: (name: string, scope: { layer_id?: string; workload_kind?: WorkloadKind } = {}) =>
      request<{ name: string }>("PUT", "/v1/admin/settings/default-warehouse", { name, ...scope }),
    clearDefaultWarehouse: (scope: { layer_id?: string | null; workload_kind?: string | null } = {}) => {
      const q = new URLSearchParams(Object.entries(scope).filter(([, v]) => v) as [string, string][]).toString();
      return request<void>("DELETE", `/v1/admin/settings/default-warehouse${q ? `?${q}` : ""}`);
    },
    workspaceSettings: () => request<WorkspaceSettings>("GET", "/v1/admin/settings/workspaces"),
    saveWorkspaceSettings: (s: WorkspaceSettings) => request<WorkspaceSettings>("PUT", "/v1/admin/settings/workspaces",
      { warehouse: s.warehouse, max_running: s.max_running, max_running_per_user: s.max_running_per_user }),
    egress: () => request<EgressAllowlist>("GET", "/v1/admin/settings/egress"),
    saveEgress: (hosts: EgressHost[]) => request<EgressAllowlist>("PUT", "/v1/admin/settings/egress", { hosts }),
    audit: () => request<AuditEvent[]>("GET", "/v1/admin/audit?limit=200"),
  },
};

/** Editors and apps open in their own window (first-party cookies; the device never holds data). Pass the
 * address as a promise and call this before awaiting anything: a browser only allows a new window while the
 * click is still being handled, so the window opens at once and gets its address when it is known. */
export async function openInWindow(url: string | Promise<string>, title: string, label: string): Promise<void> {
  if ("__TAURI_INTERNALS__" in window) {
    const target = await url;
    const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
    const existing = await WebviewWindow.getByLabel(label);
    if (existing) {
      await existing.setFocus();
      return;
    }
    new WebviewWindow(label, { url: target, title, width: 1400, height: 900 });
    return;
  }
  const opened = window.open("", label);
  try {
    const target = await url;
    if (!opened) throw new Error("The browser blocked the new window; allow pop-ups for this site and try again.");
    opened.opener = null;
    opened.location.href = target;
  } catch (e) {
    opened?.close();
    throw e;
  }
}
