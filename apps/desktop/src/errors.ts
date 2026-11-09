import { ApiError } from "./api";

/** What kind of failure an error is, so a refusal is never presented as an outage. */
export type ErrorKind = "denied" | "session" | "not-found" | "invalid" | "unavailable" | "network";

export interface Classified { kind: ErrorKind; title: string; message: string; permission?: string }

const TITLES: Record<ErrorKind, string> = {
  denied: "Your group doesn't have access",
  session: "Your session has ended",
  "not-found": "Not found",
  invalid: "This can't be done as asked",
  unavailable: "Limoon could not answer just now",
  network: "Limoon can't be reached",
};

export function classify(error: unknown): Classified {
  const message = typeof error === "string" ? error : (error as Error)?.message ?? String(error);
  const kind: ErrorKind = typeof error === "string" ? "invalid"
    : !(error instanceof ApiError) ? "network"
    : error.status === 401 ? "session"
    : error.status === 403 ? "denied"
    : error.status === 404 ? "not-found"
    : error.status >= 500 ? "unavailable" : "invalid";
  // The API names the missing permission as "<key> permission required".
  const permission = kind === "denied" ? /^([a-z][a-z0-9_.]+) permission required$/.exec(message)?.[1] : undefined;
  return { kind, title: TITLES[kind], message, permission };
}
