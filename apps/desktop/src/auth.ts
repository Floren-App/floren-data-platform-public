import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";

const SERVER_KEY = "fdp.server";
export const DEFAULT_SERVER = import.meta.env.VITE_FDP_URL ?? "https://localhost:8090";
/** Served by the platform itself (Helm web.enabled): the server is the page's own address, nothing to choose. */
export const WEB_MODE = import.meta.env.VITE_FDP_WEB === "1";

export interface ClientConfig {
  oidc_issuer: string;
  oidc_client_id: string;
  oidc_scopes: string;
  public_url: string;
}

export function getServer(): string {
  if (WEB_MODE) return window.location.origin;
  try {
    return localStorage.getItem(SERVER_KEY) ?? DEFAULT_SERVER;
  } catch {
    return DEFAULT_SERVER;
  }
}

export function setServer(url: string): void {
  try {
    localStorage.setItem(SERVER_KEY, url.replace(/\/+$/, ""));
  } catch {
    /* storage unavailable: fall back to the default server */
  }
}

let manager: UserManager | null = null;

/** The server tells us which OIDC provider to use; nothing identity-specific is baked into the app. */
export async function getManager(): Promise<UserManager> {
  if (manager) return manager;
  const res = await fetch(`${getServer()}/api/v1/client-config`);
  if (!res.ok) throw new Error(`cannot reach Floren Data Platform at ${getServer()} (${res.status})`);
  const cfg: ClientConfig = await res.json();
  manager = new UserManager({
    authority: cfg.oidc_issuer,
    client_id: cfg.oidc_client_id,
    scope: cfg.oidc_scopes,
    redirect_uri: `${window.location.origin}/`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: "code", // authorization code + PKCE (public client, no secret in the app)
    automaticSilentRenew: true,
    userStore: new WebStorageStateStore({ store: window.sessionStorage }),
  });
  return manager;
}

export function resetManager(): void {
  manager = null;
}

let pendingUser: Promise<User | null> | null = null;

/** Completes a redirect sign-in if we just came back from the IdP, else returns the cached user. */
export function currentUser(): Promise<User | null> {
  // Memoized: the authorization code is single-use and React StrictMode runs effects twice.
  pendingUser ??= loadUser().finally(() => {
    pendingUser = null;
  });
  return pendingUser;
}

async function loadUser(): Promise<User | null> {
  const mgr = await getManager();
  const params = new URLSearchParams(window.location.search);
  if (params.has("code") && params.has("state")) {
    const user = await mgr.signinRedirectCallback();
    window.history.replaceState({}, "", "/");
    return user;
  }
  const user = await mgr.getUser();
  return user && !user.expired ? user : null;
}

/** Raised when the sign-in can no longer be renewed; the app then asks the user to sign in again. */
export class SessionExpiredError extends Error {
  constructor() { super("Your session has expired. Sign in again to continue."); }
}

const expiredListeners = new Set<() => void>();
let expired = false;
export function onSessionExpired(listener: () => void): () => void {
  expiredListeners.add(listener);
  return () => { expiredListeners.delete(listener); };
}
function sessionExpired(): never {
  if (!expired) { expired = true; expiredListeners.forEach((l) => l()); }
  throw new SessionExpiredError();
}

let renewing: Promise<User | null> | null = null;
/** One renewal at a time, however many requests notice the token is about to expire. */
function renew(): Promise<User | null> {
  renewing ??= getManager().then((m) => m.signinSilent()).catch(() => null).finally(() => { renewing = null; });
  return renewing;
}

/**
 * A valid access token for an API request. Renews ahead of expiry instead of relying only on the
 * background timer, which sleep and background throttling delay; force renews after a 401.
 */
export async function accessToken(force = false): Promise<string> {
  if (expired) throw new SessionExpiredError();
  const user = await (await getManager()).getUser();
  if (!user) sessionExpired();
  if (!force && user.access_token && (user.expires_in ?? 0) > 60) return user.access_token;
  const renewed = await renew();
  if (!renewed?.access_token || renewed.expired) sessionExpired();
  return renewed.access_token;
}

// Coming back from sleep or another app: renew right away, before the next poll needs it.
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && !expired) void accessToken().catch(() => undefined);
  });
}

export async function signIn(): Promise<void> {
  await (await getManager()).signinRedirect();
}

export async function signOut(): Promise<void> {
  await (await getManager()).signoutRedirect();
}
