import * as vscode from "vscode";

/**
 * Shows a web app (Streamlit, Shiny, …) in an editor tab. The desktop app cannot open pop-up
 * windows, and "localhost" on the user's machine is not the workspace, so apps are embedded
 * through the workspace's own gateway address instead.
 */
export class AppPreview {
  private readonly panels = new Map<string, vscode.WebviewPanel>();

  show(key: string, title: string, url: string): void {
    const existing = this.panels.get(key);
    if (existing) {
      existing.webview.html = this.html(url, title);
      existing.reveal();
      return;
    }
    const panel = vscode.window.createWebviewPanel("fdp.preview", title, vscode.ViewColumn.Beside, {
      enableScripts: true,
      retainContextWhenHidden: true,
    });
    panel.webview.html = this.html(url, title);
    panel.onDidDispose(() => this.panels.delete(key));
    this.panels.set(key, panel);
  }

  /** A self-contained HTML page (dbt's static docs with lineage) in an editor tab. */
  showHtml(key: string, title: string, html: string): void {
    this.panels.get(key)?.dispose();
    const panel = vscode.window.createWebviewPanel("fdp.docs", title, vscode.ViewColumn.Active, {
      enableScripts: true,
      retainContextWhenHidden: true,
    });
    panel.webview.html = html;
    panel.onDidDispose(() => this.panels.delete(key));
    this.panels.set(key, panel);
  }

  /** A port served inside this workspace (e.g. `streamlit run` in the terminal). */
  async showPort(port: number, title = `Preview :${port}`): Promise<void> {
    // code-server maps localhost ports to <workspace URL>/proxy/<port>/ on the user's side.
    const external = await vscode.env.asExternalUri(vscode.Uri.parse(`http://localhost:${port}/`));
    this.show(`port-${port}`, title, external.toString(true));
  }

  private html(url: string, title: string): string {
    const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
    const origin = new URL(url).origin;
    return `<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src ${origin}; style-src 'unsafe-inline';">
<style>
  html, body { margin: 0; padding: 0; height: 100%; overflow: hidden; background: var(--vscode-editor-background); }
  .bar { display: flex; gap: 8px; align-items: center; height: 26px; padding: 0 8px; font: 12px var(--vscode-font-family);
         color: var(--vscode-descriptionForeground); border-bottom: 1px solid var(--vscode-panel-border); }
  .bar a { color: var(--vscode-textLink-foreground); }
  iframe { border: 0; width: 100%; height: calc(100% - 27px); background: #fff; }
</style></head>
<body><div class="bar"><span>${esc(title)}</span><span style="flex:1"></span>
<a href="${esc(url)}" title="Reload">reload</a></div>
<iframe src="${esc(url)}" title="${esc(title)}" allow="clipboard-read; clipboard-write; fullscreen"></iframe></body></html>`;
  }
}
