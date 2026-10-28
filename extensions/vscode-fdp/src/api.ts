// Talks to the Floren Data Platform control plane with this workspace's session token (injected by Floren Data Platform).

export interface Arg {
  name: string;
  type: "string" | "text" | "integer" | "number" | "boolean" | "choice" | "date" | "data_source";
  label?: string | null;
  help?: string | null;
  required?: boolean;
  default?: string | number | boolean | null;
  choices?: string[] | null;
  min?: number | null;
  max?: number | null;
}

export interface Program {
  description: string;
  layer?: string | null;
  kind: "script" | "notebook" | "app" | "dbt" | "airflow";
  entrypoint: string;
  runtime: "python" | "r";
  requirements?: string | null;
  warehouse?: string | null;
  data_sources: string[];
  args: Arg[];
}

export interface ConfigOut {
  exists: boolean;
  live: boolean;
  errors: string[];
  config: { warehouse?: string | null; programs: Record<string, Program> } | null;
}

export interface Pool { name: string; cpu: number; memory_gb: number; gpu: number }
export interface Source { name: string; kind: string; description: string }

export interface Run {
  id: string;
  kind: string;
  entrypoint: string;
  program: string | null;
  args: Record<string, unknown>;
  runtime: string | null;
  runtime_version: string | null;
  status: "pending" | "running" | "succeeded" | "failed" | "stopped";
  pool_name: string;
  created_at: string;
  finished_at: string | null;
}

export interface OutputFile { path: string; size: number }

export class ApiError extends Error {
  constructor(public status: number, message: string, public fieldErrors?: Record<string, string>) {
    super(message);
  }
}

export const ACTIVE = new Set(["pending", "running"]);

export class Api {
  readonly base = process.env.FDP_API ?? "";
  readonly workspaceId = process.env.FDP_WORKSPACE_ID ?? "";
  private readonly token = process.env.FDP_SESSION_TOKEN ?? "";

  get available(): boolean {
    return Boolean(this.base && this.token && this.workspaceId);
  }

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await fetch(`${this.base}${path}`, {
      method,
      headers: { Authorization: `Bearer ${this.token}`, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!res.ok) {
      let detail: unknown = res.statusText;
      try {
        detail = ((await res.json()) as { detail?: unknown }).detail ?? detail;
      } catch {
        /* not JSON */
      }
      if (detail && typeof detail === "object" && "errors" in detail) {
        const d = detail as { message?: string; errors: Record<string, string> | string[] };
        if (Array.isArray(d.errors)) throw new ApiError(res.status, `${d.message}: ${d.errors.join("; ")}`);
        throw new ApiError(res.status, d.message ?? "invalid request", d.errors);
      }
      throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    const type = res.headers.get("content-type") ?? "";
    return (type.includes("json") ? res.json() : res.text()) as Promise<T>;
  }

  config = () => this.req<ConfigOut>("GET", `/v1/workspaces/${this.workspaceId}/config`);
  pools = () => this.req<Pool[]>("GET", "/v1/pools");
  sources = () => this.req<Source[]>("GET", "/v1/connect");
  runs = () => this.req<Run[]>("GET", `/v1/runs?workspace_id=${this.workspaceId}`);
  run = (id: string) => this.req<Run>("GET", `/v1/runs/${id}`);
  logs = (id: string) => this.req<string>("GET", `/v1/runs/${id}/logs?tail=20000`);
  outputs = (id: string) => this.req<OutputFile[]>("GET", `/v1/runs/${id}/outputs`);
  stop = (id: string) => this.req<Run>("POST", `/v1/runs/${id}/stop`);
  openApp = (id: string) => this.req<{ url: string }>("POST", `/v1/runs/${id}/open`);
  start = (program: string, args: Record<string, unknown>, pool?: string) =>
    this.req<Run>("POST", "/v1/runs", { workspace_id: this.workspaceId, program, args, ...(pool ? { pool } : {}) });
}
