import * as crypto from "crypto";
import * as vscode from "vscode";
import { Api, ApiError, Program, Run, Source } from "./api";
import { RunConsoles } from "./console";
import { AppPreview } from "./preview";
import { insertSnippet } from "./snippets";

const WORKSPACE = "/workspace";
const CONFIG = vscode.Uri.file(`${WORKSPACE}/fdp.json`);

const STARTER = {
  version: 1,
  warehouse: "small",
  programs: {
    hello: {
      description: "Example program: replace with your own",
      entrypoint: "hello.py",
      requirements: "requirements.txt",
      args: [{ name: "name", type: "string", default: "world", help: "Who to greet" }],
    },
  },
};

export function activate(context: vscode.ExtensionContext): void {
  const api = new Api();
  const preview = new AppPreview();
  const provider = new RunnerView(context, api, preview);
  context.subscriptions.push(
    vscode.commands.registerCommand("fdp.previewPort", async () => {
      const value = await vscode.window.showInputBox({
        title: "Preview an app running in this workspace",
        prompt: "Port the app listens on (Streamlit 8501, Shiny 3838, Dash 8050, …)",
        value: "8501",
        validateInput: (v) => (/^\d{2,5}$/.test(v) ? undefined : "Enter a port number"),
      });
      if (value) await preview.showPort(Number(value));
    }),
    vscode.window.registerWebviewViewProvider("fdp.runner", provider, { webviewOptions: { retainContextWhenHidden: true } }),
    vscode.commands.registerCommand("fdp.refresh", () => provider.refresh()),
    vscode.commands.registerCommand("fdp.openConfig", () => openConfig()),
    vscode.commands.registerCommand("fdp.createConfig", () => createConfig()),
    vscode.commands.registerCommand("fdp.showRunner", () => vscode.commands.executeCommand("fdp.runner.focus")),
  );

  // Re-read programs whenever fdp.json is saved.
  const watcher = vscode.workspace.createFileSystemWatcher(new vscode.RelativePattern(WORKSPACE, "fdp.json"));
  watcher.onDidChange(() => provider.refresh());
  watcher.onDidCreate(() => provider.refresh());
  watcher.onDidDelete(() => provider.refresh());
  context.subscriptions.push(watcher);

  const status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
  status.text = "$(play-circle) Floren Data Platform";
  status.tooltip = "Run programs on a warehouse";
  status.command = "fdp.showRunner";
  status.show();
  context.subscriptions.push(status);
}

async function openConfig(): Promise<void> {
  await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(CONFIG));
}

async function createConfig(): Promise<void> {
  try {
    await vscode.workspace.fs.stat(CONFIG);
  } catch {
    const enc = new TextEncoder();
    await vscode.workspace.fs.writeFile(CONFIG, enc.encode(JSON.stringify(STARTER, null, 2) + "\n"));
    const hello = vscode.Uri.file(`${WORKSPACE}/hello.py`);
    try {
      await vscode.workspace.fs.stat(hello);
    } catch {
      await vscode.workspace.fs.writeFile(hello, enc.encode(
        'import fdp\n\nargs = fdp.args()\nprint(f"Hello, {args[\'name\']}!")\n'));
    }
  }
  await openConfig();
}

class RunnerView implements vscode.WebviewViewProvider {
  private view?: vscode.WebviewView;
  private readonly consoles: RunConsoles;
  private poll?: NodeJS.Timeout;

  constructor(private readonly context: vscode.ExtensionContext, private readonly api: Api,
              private readonly preview: AppPreview) {
    this.consoles = new RunConsoles(api, () => this.refreshRuns());
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.context.extensionUri, "media");
    view.webview.options = { enableScripts: true, localResourceRoots: [media] };
    view.webview.html = this.html(view.webview, media);
    view.webview.onDidReceiveMessage((m) => this.onMessage(m));
    view.onDidChangeVisibility(() => this.schedule());
    view.onDidDispose(() => this.poll && clearTimeout(this.poll));
  }

  private post(message: unknown): void {
    this.view?.webview.postMessage(message);
  }

  async refresh(): Promise<void> {
    if (!this.api.available) {
      this.post({ type: "unavailable" });
      return;
    }
    try {
      const [config, pools, sources, runs] = await Promise.all([
        this.api.config(), this.api.pools(), this.api.sources(), this.api.runs(),
      ]);
      this.post({ type: "state", config, pools, sources, runs });
    } catch (e) {
      this.post({ type: "error", message: (e as Error).message });
    }
    this.schedule();
  }

  private async refreshRuns(): Promise<void> {
    try {
      this.post({ type: "runs", runs: await this.api.runs() });
    } catch {
      /* next poll retries */
    }
  }

  /** Poll runs while the panel is visible: faster when something is running. */
  private schedule(): void {
    if (this.poll) clearTimeout(this.poll);
    if (!this.view?.visible) return;
    this.poll = setTimeout(async () => {
      await this.refreshRuns();
      this.schedule();
    }, 3000);
  }

  private async onMessage(m: { type: string; [k: string]: unknown }): Promise<void> {
    try {
      switch (m.type) {
        case "ready":
        case "refresh":
          return this.refresh();
        case "openConfig":
          return openConfig();
        case "createConfig":
          return createConfig();
        case "run": {
          const run = await this.api.start(m.program as string, m.args as Record<string, unknown>, m.pool as string);
          this.post({ type: "started", run });
          this.consoles.show(run);
          return this.refreshRuns();
        }
        case "console":
          return this.consoles.show(m.run as Run);
        case "stop":
          await this.api.stop(m.id as string);
          return this.refreshRuns();
        case "outputs": {
          const uri = vscode.Uri.file(`${WORKSPACE}/.fdp/runs/${m.id}/outputs`);
          await vscode.commands.executeCommand("revealInExplorer", uri);
          return;
        }
        case "insertSource":
          return insertSnippet(m.source as Source);
        case "openApp": {
          // Warehouse app: one-time ticket URL, embedded in an editor tab (no pop-up windows).
          const { url } = await this.api.openApp(m.id as string);
          this.preview.show(`run-${m.id}`, `${m.title ?? "App"} (warehouse)`, url);
          return;
        }
        case "docs": {
          const file = vscode.Uri.file(`${WORKSPACE}/.fdp/runs/${m.id}/outputs/docs/index.html`);
          try {
            const html = new TextDecoder().decode(await vscode.workspace.fs.readFile(file));
            this.preview.showHtml(`docs-${m.id}`, `dbt docs · ${m.title ?? m.id}`, html);
          } catch {
            vscode.window.showWarningMessage("This run produced no dbt docs (did dbt fail before generating them?).");
          }
          return;
        }
        case "previewLocal":
          return this.previewLocal(m.program as string, m.prog as Program);
      }
    } catch (e) {
      const err = e as ApiError;
      this.post({ type: "error", message: err.message, fieldErrors: err.fieldErrors });
    }
  }

  /** Quick UI iteration: run a Streamlit app inside the (small) workspace and preview it in a tab. */
  private async previewLocal(name: string, prog: Program): Promise<void> {
    if (prog.runtime !== "python") {
      vscode.window.showInformationMessage("Shiny apps preview best in RStudio's Viewer; or use “Run” to start it on a warehouse.");
      return;
    }
    const port = 8501;
    const term = vscode.window.terminals.find((t) => t.name === `preview: ${name}`)
      ?? vscode.window.createTerminal({ name: `preview: ${name}`, cwd: WORKSPACE });
    term.show(true);
    term.sendText(`streamlit run ${JSON.stringify(prog.entrypoint)} --server.port ${port} --server.headless true`);
    // Give Streamlit a moment to bind before the preview loads.
    for (let i = 0; i < 40; i++) {
      try {
        const res = await fetch(`http://127.0.0.1:${port}/_stcore/health`);
        if (res.ok) break;
      } catch {
        /* not up yet */
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    await this.preview.showPort(port, `${name} (preview)`);
  }

  private html(webview: vscode.Webview, media: vscode.Uri): string {
    const nonce = crypto.randomBytes(16).toString("base64");
    const css = webview.asWebviewUri(vscode.Uri.joinPath(media, "runner.css"));
    const js = webview.asWebviewUri(vscode.Uri.joinPath(media, "runner.js"));
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
<link rel="stylesheet" href="${css}"></head>
<body><div id="app"><p class="muted">Loading…</p></div>
<script nonce="${nonce}" src="${js}"></script></body></html>`;
  }
}

export function deactivate(): void {
  /* nothing to clean up */
}
