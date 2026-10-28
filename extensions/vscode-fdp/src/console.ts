import * as vscode from "vscode";
import { ACTIVE, Api, Run } from "./api";

const POLL_MS = 1500;
const DIM = "\x1b[2m", RESET = "\x1b[0m", GREEN = "\x1b[32m", RED = "\x1b[31m", YELLOW = "\x1b[33m";

/** A terminal that streams a warehouse run's console. Ctrl+C stops the run. */
export class RunConsoles {
  private readonly open = new Map<string, vscode.Terminal>();

  constructor(private readonly api: Api, private readonly onChange: () => void) {
    vscode.window.onDidCloseTerminal((t) => {
      for (const [id, term] of this.open) if (term === t) this.open.delete(id);
    });
  }

  show(run: Run): void {
    const existing = this.open.get(run.id);
    if (existing) {
      existing.show();
      return;
    }
    const write = new vscode.EventEmitter<string>();
    let printed = 0;
    let timer: NodeJS.Timeout | undefined;
    let finished = false;
    const title = run.program ?? run.entrypoint;

    const poll = async () => {
      try {
        const [logs, current] = await Promise.all([this.api.logs(run.id), this.api.run(run.id)]);
        if (logs.length > printed) {
          write.fire(logs.slice(printed).replace(/\r?\n/g, "\r\n"));
          printed = logs.length;
        }
        if (!ACTIVE.has(current.status)) {
          finished = true;
          const color = current.status === "succeeded" ? GREEN : current.status === "stopped" ? YELLOW : RED;
          write.fire(`\r\n${color}● ${current.status}${RESET}${DIM}  (outputs: .fdp/runs/${run.id}/outputs)${RESET}\r\n`);
          this.onChange();
          return;
        }
      } catch (e) {
        write.fire(`\r\n${RED}[fdp] ${(e as Error).message}${RESET}\r\n`);
      }
      timer = setTimeout(poll, POLL_MS);
    };

    const pty: vscode.Pseudoterminal = {
      onDidWrite: write.event,
      open: () => {
        write.fire(`${DIM}${title} on warehouse ${run.pool_name} · run ${run.id} · Ctrl+C stops the run${RESET}\r\n\r\n`);
        poll();
      },
      close: () => timer && clearTimeout(timer),
      handleInput: async (data) => {
        if (data === "\x03" && !finished) {
          write.fire(`\r\n${YELLOW}[fdp] stopping...${RESET}\r\n`);
          await this.api.stop(run.id).catch((e: Error) => write.fire(`${RED}${e.message}${RESET}\r\n`));
          this.onChange();
        }
      },
    };
    const term = vscode.window.createTerminal({ name: `▶ ${title} (${run.pool_name})`, pty });
    this.open.set(run.id, term);
    term.show();
  }
}
