import { useCallback, useEffect, useState } from "react";
import { api, ApiError, ArgDef, programRuntime, ProgramDef, Run, runtimeLabel, startingPool, usablePools, WorkspaceConfig } from "../api";
import type { Hub } from "../hub";
import { IconPlay, IconRefresh } from "../components/Icons";

import { AccessError } from "../components/AccessError";
import type { OpenTab } from "../components/Permissions";

type Values = Record<string, string | boolean>;

const initial = (p: ProgramDef): Values =>
  Object.fromEntries(p.args.map((a) => [a.name, a.default ?? (a.type === "boolean" ? false : "")])) as Values;

/** Typed values for the API; empty fields are left out so defaults and "required" apply server-side. */
function collect(p: ProgramDef, v: Values): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const a of p.args) {
    const x = v[a.name];
    if (x === "" || x === undefined) continue;
    out[a.name] = a.type === "integer" ? parseInt(String(x), 10) : a.type === "number" ? parseFloat(String(x)) : x;
  }
  return out;
}

export function ArgField({ arg, value, onChange, error, sources }: {
  arg: ArgDef; value: string | boolean; onChange: (v: string | boolean) => void; error?: string; sources: string[];
}) {
  const id = `arg-${arg.name}`;
  const label = `${arg.label || arg.name}${arg.required ? " *" : ""}`;
  let input: JSX.Element;
  if (arg.type === "boolean") {
    return (
      <div className="field">
        <label className="row" style={{ minHeight: 26, marginTop: 18 }}>
          <input type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} /> {label}
        </label>
        {arg.help && <span className="hint">{arg.help}</span>}
        {error && <span className="field-error">{error}</span>}
      </div>
    );
  }
  if (arg.type === "choice" || arg.type === "data_source") {
    const options = arg.type === "choice" ? arg.choices ?? [] : sources;
    input = (
      <select id={id} value={String(value)} onChange={(e) => onChange(e.target.value)}>
        {!arg.required && <option value="">—</option>}
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  } else {
    const type = arg.type === "integer" || arg.type === "number" ? "number" : arg.type === "date" ? "date" : "text";
    input = (
      <input id={id} type={type} value={String(value)} onChange={(e) => onChange(e.target.value)}
             min={arg.min ?? undefined} max={arg.max ?? undefined}
             step={arg.type === "integer" ? 1 : arg.type === "number" ? "any" : undefined} />
    );
  }
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {input}
      {arg.help && <span className="hint">{arg.help}</span>}
      {error && <span className="field-error">{error}</span>}
    </div>
  );
}

export function useWorkspaceConfig(workspaceId: string, running: boolean) {
  const [cfg, setCfg] = useState<WorkspaceConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(() => {
    api.workspaceConfig(workspaceId).then((c) => { setCfg(c); setError(null); }).catch((e: Error) => setError(e.message));
  }, [workspaceId]);
  useEffect(() => { load(); }, [load, running]);
  return { cfg, error, reload: load };
}

export function ProgramRunner({ workspaceId, running, hub, onStarted, openTab }: {
  workspaceId: string; running: boolean; hub: Hub; onStarted: (run: Run) => void; openTab: OpenTab;
}) {
  const { cfg, error, reload } = useWorkspaceConfig(workspaceId, running);
  const programs = cfg?.config?.programs ?? {};
  const names = Object.keys(programs);
  const [selected, setSelected] = useState<string>("");
  const name = programs[selected] ? selected : names[0] ?? "";
  const program = programs[name];
  const [values, setValues] = useState<Values>({});
  const [pool, setPool] = useState("");
  const [busy, setBusy] = useState(false);
  const [runError, setRunError] = useState<Error | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const declared = program?.warehouse || cfg?.config?.warehouse || "";
  const layerId = hub.workspaces.find((w) => w.id === workspaceId)?.layer_id;
  const defaultPool = startingPool(hub.me?.pools, declared, layerId, program?.kind);
  const missing = declared && !usablePools(hub.me?.pools, layerId, program?.kind).some((p) => p.name === declared);
  useEffect(() => {
    if (program) {
      setValues(initial(program));
      setPool(startingPool(hub.me?.pools, program.warehouse || cfg?.config?.warehouse || "", layerId, program.kind));
      setFieldErrors({});
      setRunError(null);
    }
  }, [name, program, cfg?.config?.warehouse]);

  const run = async () => {
    setBusy(true);
    setRunError(null);
    setFieldErrors({});
    try {
      const r = await api.runProgram(workspaceId, name, collect(program, values), pool || undefined);
      await hub.refresh();
      onStarted(r);
    } catch (e) {
      setRunError(e as Error);
      if (e instanceof ApiError) setFieldErrors(e.fieldErrors);
    }
    setBusy(false);
  };

  const header = (
    <div className="section-heading" style={{ margin: 0 }}>
      <h2>Programs</h2>
      <span className="row muted">
        {cfg && !cfg.live && cfg.exists && <span className="badge" title="Workspace is stopped: showing the last copy of fdp.json">cached</span>}
        <button className="icon-btn" title="Reload fdp.json" aria-label="Reload fdp.json" onClick={reload}><IconRefresh /></button>
      </span>
    </div>
  );

  if (error) return <div className="card">{header}<p className="error" role="alert">{error}</p></div>;
  if (!cfg) return <div className="card">{header}<p className="muted">Loading…</p></div>;
  if (!cfg.exists) {
    return (
      <div className="card">
        {header}
        <p className="muted">
          {running
            ? <>No <code>fdp.json</code> in this workspace yet. Create one from the editor (VS Code: <i>Floren Data Platform: Create fdp.json</i>; RStudio: <code>fdp::init()</code>) to list the programs you can run here.</>
            : <>Start the workspace to read its <code>fdp.json</code>.</>}
        </p>
      </div>
    );
  }
  // A broken file only hides its own programs; the others stay runnable.
  const problems = cfg.errors.length > 0 && (
    <>
      <div className="error"><b>fdp.json has problems:</b>
        <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>{cfg.errors.map((e) => <li key={e}>{e}</li>)}</ul></div>
    </>
  );
  if (!program) return <div className="card">{header}{problems || <p className="muted">No programs are available for this workspace's layer.</p>}</div>;

  return (
    <div className="card">
      {header}
      {problems}
      <div className="program-tabs" role="tablist">
        {names.map((n) => (
          <button key={n} role="tab" aria-selected={n === name} className={`chip ${n === name ? "active" : ""}`}
                  onClick={() => setSelected(n)}>{n}</button>
        ))}
      </div>
      <p className="hint">Only programs for this workspace's layer are shown.</p>
      {program.description && <p>{program.description}</p>}
      <p className="muted mono" style={{ marginTop: 0 }}>
        {program.kind} · {runtimeLabel(programRuntime(program).kind, programRuntime(program).version ?? "default")} · {program.entrypoint}
        {program.layer && ` · layer: ${hub.layers.find((l) => l.id === program.layer)?.name || program.layer}`}
        {program.dbt && ` · builds in ${program.dbt.data_source} (your dev schema)`}
        {program.requirements && ` · ${program.requirements}`}
        {program.data_sources.length > 0 && ` · data: ${program.data_sources.join(", ")}`}
      </p>
      <div className="arg-grid">
        {program.args.map((a) => (
          <ArgField key={`${name}.${a.name}`} arg={a} value={values[a.name] ?? ""} error={fieldErrors[a.name]}
                    sources={hub.me?.data_sources.map((d) => d.name) ?? []}
                    onChange={(v) => setValues({ ...values, [a.name]: v })} />
        ))}
        <div className="field">
          <label htmlFor="program-pool">Warehouse</label>
          <select id="program-pool" value={pool} onChange={(e) => setPool(e.target.value)}>
            {!pool && <option value="">— choose —</option>}
            {usablePools(hub.me?.pools, layerId, program?.kind).map((p) => (
              <option key={p.id} value={p.name}>
                {p.name} · {p.cpu} vCPU · {p.memory_gb} GB{p.gpu ? ` · ${p.gpu} GPU` : ""}{p.default ? " (default)" : ""}
              </option>
            ))}
          </select>
          {missing && <span className="hint">fdp.json asks for warehouse {declared}, which does not exist here; {defaultPool ? `using the default warehouse ${defaultPool}` : "choose one"}.</span>}
        </div>
      </div>
      {runError && <AccessError error={runError} openTab={openTab} />}
      <div className="form-actions" style={{ marginTop: 4 }}>
        <button className="btn" onClick={run} disabled={busy || (!pool && !defaultPool)}>
          <IconPlay size={14} />{busy ? "Starting…" : program.kind === "airflow" ? `Start test deployment of ${name}` : `Run ${name}`}
        </button>
      </div>
    </div>
  );
}
