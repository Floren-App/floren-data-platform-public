// Runner panel UI: pick a program from fdp.json, fill its arguments, choose a warehouse, run.
(function () {
  const vscode = acquireVsCodeApi();
  const app = document.getElementById("app");
  const ACTIVE = new Set(["pending", "running"]);
  let state = { config: null, pools: [], sources: [], runs: [] };
  let selected = (vscode.getState() || {}).selected || null;
  const values = (vscode.getState() || {}).values || {};
  let error = null;
  let fieldErrors = {};
  let busy = false;

  const h = (tag, attrs, ...children) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c != null && c !== false) el.append(c.nodeType ? c : String(c));
    return el;
  };
  const save = () => vscode.setState({ selected, values });
  const runtimeOf = (p) => {
    const [name, v] = p.kind === "dbt" ? ["dbt", p.dbt_version] : p.kind === "airflow" ? ["Airflow", p.airflow]
      : p.runtime === "r" ? ["R", p.r] : ["Python", p.python];
    return `${name} ${v || "(default)"}`;
  };
  const programs = () => (state.config && state.config.config && state.config.config.programs) || {};

  window.addEventListener("message", (e) => {
    const m = e.data;
    if (m.type === "state") { state = m; error = null; }
    else if (m.type === "runs") state.runs = m.runs;
    else if (m.type === "error") { error = m.message; fieldErrors = m.fieldErrors || {}; busy = false; }
    else if (m.type === "started") { busy = false; fieldErrors = {}; error = null; }
    else if (m.type === "unavailable") {
      app.replaceChildren(h("p", { class: "muted" }, "Floren Data Platform is not available in this window (no session token)."));
      return;
    }
    render();
  });

  function argField(prog, a) {
    const key = `${selected}.${a.name}`;
    const current = key in values ? values[key] : a.default ?? "";
    const set = (v) => { values[key] = v; save(); };
    const label = a.label || a.name;
    const req = a.required ? " *" : "";
    let input;
    if (a.type === "boolean") {
      input = h("input", { type: "checkbox", id: key, checked: current === true || current === "true",
        onchange: (e) => set(e.target.checked) });
      return h("div", {}, h("div", { class: "check" }, input, h("label", { for: key, style: "margin:0" }, label + req)),
        a.help && h("div", { class: "help" }, a.help), fieldErrors[a.name] && h("div", { class: "field-error" }, fieldErrors[a.name]));
    }
    if (a.type === "choice" || a.type === "data_source") {
      const opts = a.type === "choice" ? a.choices : state.sources.map((s) => s.name);
      input = h("select", { id: key, onchange: (e) => set(e.target.value) },
        !a.required && h("option", { value: "" }, "—"),
        opts.map((o) => h("option", { value: o, selected: String(current) === o }, o)));
    } else {
      const type = { integer: "number", number: "number", date: "date" }[a.type] || "text";
      input = h("input", { id: key, type, value: current, min: a.min, max: a.max,
        step: a.type === "integer" ? 1 : a.type === "number" ? "any" : null,
        oninput: (e) => set(e.target.value) });
    }
    return h("div", {}, h("label", { for: key }, label + req), input,
      a.help && h("div", { class: "help" }, a.help), fieldErrors[a.name] && h("div", { class: "field-error" }, fieldErrors[a.name]));
  }

  function collect(prog) {
    const out = {};
    for (const a of prog.args) {
      const key = `${selected}.${a.name}`;
      const v = key in values ? values[key] : a.default;
      if (v === "" || v == null) continue;
      out[a.name] = a.type === "integer" ? parseInt(v, 10) : a.type === "number" ? parseFloat(v) : v;
    }
    return out;
  }

  function render() {
    const cfg = state.config;
    const progs = programs();
    const names = Object.keys(progs);
    if (!selected || !progs[selected]) selected = names[0] || null;

    const head = h("h3", {}, "Programs", h("span", {},
      h("button", { class: "link", title: "Edit fdp.json", onclick: () => vscode.postMessage({ type: "openConfig" }) }, "edit"),
      h("button", { class: "link", title: "Reload", onclick: () => vscode.postMessage({ type: "refresh" }) }, "reload")));

    const body = [];
    if (!cfg) body.push(h("p", { class: "muted" }, "Loading…"));
    else if (!cfg.exists) {
      body.push(h("p", { class: "muted" }, "This workspace has no fdp.json yet. It lists the programs you can run on a warehouse, with their arguments and requirements."),
        h("button", { onclick: () => vscode.postMessage({ type: "createConfig" }) }, "Create fdp.json"));
    } else if (!names.length) {
      if (cfg.errors.length) {
        body.push(h("div", { class: "error" }, "fdp.json has problems:\n• " + cfg.errors.join("\n• ")),
          h("button", { class: "secondary", onclick: () => vscode.postMessage({ type: "openConfig" }) }, "Open fdp.json"));
      } else body.push(h("p", { class: "muted" }, "No programs defined yet."));
    } else {
      // A broken file only hides its own programs; the others stay runnable.
      if (cfg.errors.length) body.push(h("div", { class: "error" }, "fdp.json has problems:\n• " + cfg.errors.join("\n• ")));
      const prog = progs[selected];
      const defaultPool = prog.warehouse || cfg.config.warehouse || "";
      const poolKey = `${selected}.__pool`;
      const pool = values[poolKey] || defaultPool;
      body.push(
        h("label", { for: "program" }, "Program"),
        h("select", { id: "program", onchange: (e) => { selected = e.target.value; fieldErrors = {}; save(); render(); } },
          names.map((n) => h("option", { value: n, selected: n === selected }, n))),
        prog.description && h("p", { class: "desc" }, prog.description),
        h("div", { class: "meta" }, `${prog.kind} · ${runtimeOf(prog)} · ${prog.entrypoint}`,
          prog.requirements ? ` · ${prog.requirements}` : "",
          prog.data_sources.length ? ` · data: ${prog.data_sources.join(", ")}` : ""),
        prog.args.map((a) => argField(prog, a)),
        h("label", { for: "pool" }, "Warehouse"),
        h("select", { id: "pool", onchange: (e) => { values[poolKey] = e.target.value; save(); } },
          state.pools.map((p) => h("option", { value: p.name, selected: p.name === pool },
            `${p.name} · ${p.cpu} vCPU · ${p.memory_gb} GB${p.gpu ? ` · ${p.gpu} GPU` : ""}${p.name === defaultPool ? " (default)" : ""}`))),
        h("button", { class: "run-btn", disabled: busy, onclick: () => {
          busy = true; error = null; fieldErrors = {}; render();
          vscode.postMessage({ type: "run", program: selected, args: collect(prog), pool: values[poolKey] || defaultPool });
        } }, busy ? "Starting…" : prog.kind === "airflow" ? `▶ Start Airflow test deployment` : `▶ Run ${selected} on warehouse`),
        prog.kind === "dbt" && prog.dbt && h("div", { class: "help" }, `Builds in ${prog.dbt.data_source}, in your own dev schema. Docs with lineage are generated after every run.`),
        prog.kind === "app" && prog.runtime === "python" && h("button", { class: "run-btn secondary",
          title: "Run Streamlit inside this workspace and show it in an editor tab (quick UI iteration)",
          onclick: () => vscode.postMessage({ type: "previewLocal", program: selected, prog }) }, "Preview here"),
      );
    }
    if (error) body.push(h("div", { class: "error", role: "alert" }, error));

    const runs = state.runs.slice(0, 15);
    const runList = h("ul", { class: "runs" }, runs.map((r) => {
      const live = ACTIVE.has(r.status);
      const when = new Date(r.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      return h("li", {},
        h("div", { class: "top" }, h("span", { class: `dot ${r.status}`, title: r.status }),
          h("span", { class: "name", title: r.entrypoint }, r.program || r.entrypoint),
          h("span", { class: "muted" }, `${r.pool_name} · ${when}`)),
        h("div", { class: "actions" },
          h("button", { class: "link", onclick: () => vscode.postMessage({ type: "console", run: r }) }, "console"),
          live && h("button", { class: "link", onclick: () => vscode.postMessage({ type: "stop", id: r.id }) }, "■ stop"),
          !live && h("button", { class: "link", onclick: () => vscode.postMessage({ type: "outputs", id: r.id }) }, "outputs"),
          live && (r.kind === "app" || r.kind === "airflow") && h("button", { class: "link",
            onclick: () => vscode.postMessage({ type: "openApp", id: r.id, title: r.program || r.entrypoint }) },
            r.kind === "airflow" ? "open Airflow" : "open app"),
          !live && r.kind === "dbt" && h("button", { class: "link",
            onclick: () => vscode.postMessage({ type: "docs", id: r.id, title: r.program || r.entrypoint }) }, "docs")));
    }));

    // Data products granted to this user, with a one-click snippet into the open file.
    const data = state.sources.length
      ? h("ul", { class: "sources" }, state.sources.map((s) => h("li", {},
          h("div", { class: "top" },
            h("span", { class: "name", title: s.description || s.name }, s.name),
            h("span", { class: "badge" }, s.kind),
            h("button", { class: "secondary small", title: `Insert code to read ${s.name} at the cursor`,
              onclick: () => vscode.postMessage({ type: "insertSource", source: s }) }, "+ Insert")),
          s.description && h("div", { class: "muted small-text" }, s.description))))
      : h("p", { class: "muted" }, "No data sources granted to you yet. Ask an admin.");

    // body mixes nodes, lists of nodes (argument fields) and skipped conditionals (false/null/"").
    const nodes = body.flat().filter((n) => n != null && n !== false && n !== "");
    app.replaceChildren(head, ...nodes, h("h3", {}, "Data"), data, h("h3", {}, "Runs"),
      runs.length ? runList : h("p", { class: "muted" }, "No runs yet."));
  }

  vscode.postMessage({ type: "ready" });
})();
