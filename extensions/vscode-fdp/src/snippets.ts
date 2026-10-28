import * as vscode from "vscode";
import type { Source } from "./api";

const SQL = new Set(["postgres", "mysql", "mssql", "snowflake", "trino"]);

type Lang = "python" | "r";

export interface Imports { fdp: boolean; pandas: boolean }

/** Which imports a file already has, so the snippet doesn't repeat them. */
export function importsIn(text: string): Imports {
  return { fdp: /^\s*import fdp\b/m.test(text), pandas: /^\s*import pandas as pd\b/m.test(text) };
}

/** Snippet to access a data source; $1 lands on the query so it can be typed over. */
export function snippetFor(src: Source, lang: Lang, have: Imports = { fdp: false, pandas: false }): string {
  const name = JSON.stringify(src.name);
  const v = src.name.replace(/[^A-Za-z0-9]+/g, "_").replace(/^(\d)/, "_$1");
  if (lang === "python") {
    const lines = [!have.fdp && "import fdp", SQL.has(src.kind) && !have.pandas && "import pandas as pd"].filter(Boolean);
    const imports = lines.length ? `${lines.join("\n")}\n\n` : "";
    if (SQL.has(src.kind)) {
      return `${imports}${v} = fdp.connect(${name})\ndf = pd.read_sql("\${1:select * from table limit 100}", ${v})\n$0`;
    }
    if (src.kind === "s3") {
      return `${imports}${v} = fdp.connect(${name})  # boto3 S3 client\n${v}.list_objects_v2(Bucket="\${1:bucket}")\n$0`;
    }
    return `${imports}${v} = fdp.credentials(${name})  # {"kind", "config", "credentials"}\n$0`;
  }
  if (SQL.has(src.kind)) {
    return `${v} <- fdp::connect(${name})\ndf <- DBI::dbGetQuery(${v}, "\${1:select * from table limit 100}")\n$0`;
  }
  return `${v} <- fdp::credentials(${name})  # list(kind, config, credentials)\n$0`;
}

function langOf(languageId: string): Lang | undefined {
  if (languageId === "python") return "python";
  if (languageId === "r" || languageId === "rmd" || languageId === "quarto") return "r";
  return undefined;
}

/** Inserts at the cursor of the last active editor, or as a new cell after the selected notebook cell. */
export async function insertSnippet(src: Source): Promise<void> {
  const nb = vscode.window.activeNotebookEditor;
  if (nb) {
    const cells = nb.notebook.getCells();
    const lang = langOf(cells.find((c) => c.kind === vscode.NotebookCellKind.Code)?.document.languageId ?? "python") ?? "python";
    const have = importsIn(cells.map((c) => c.document.getText()).join("\n"));
    const code = snippetFor(src, lang, have)
      .replace(/\$\{\d+:([^}]*)\}/g, "$1").replace(/\$\d+/g, "");   // cells get plain text
    const at = nb.selection.end;
    const edit = new vscode.WorkspaceEdit();
    edit.set(nb.notebook.uri, [vscode.NotebookEdit.insertCells(at, [
      new vscode.NotebookCellData(vscode.NotebookCellKind.Code, code.trimEnd(), lang),
    ])]);
    await vscode.workspace.applyEdit(edit);
    return;
  }

  const editor = vscode.window.activeTextEditor ?? vscode.window.visibleTextEditors[0];
  const lang = editor && langOf(editor.document.languageId);
  if (!editor || !lang) {
    const python = snippetFor(src, "python").replace(/\$\{\d+:([^}]*)\}/g, "$1").replace(/\$\d+/g, "");
    const pick = await vscode.window.showInformationMessage(
      `Open a Python or R file (or a notebook) to insert the snippet for ${src.name}.`, "Copy Python snippet");
    if (pick) await vscode.env.clipboard.writeText(python);
    return;
  }
  const have = importsIn(editor.document.getText());
  // Start on a fresh line so the snippet never lands in the middle of existing code.
  const line = editor.document.lineAt(editor.selection.active.line);
  const prefix = line.isEmptyOrWhitespace ? "" : "\n";
  const pos = line.isEmptyOrWhitespace ? editor.selection.active : line.range.end;
  await vscode.window.showTextDocument(editor.document, editor.viewColumn);
  await editor.insertSnippet(new vscode.SnippetString(prefix + snippetFor(src, lang, have)), pos);
}
