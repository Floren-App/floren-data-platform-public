import type { TabKind, TabRef } from "./views/Tabs";
import { TAB_PAGES } from "./permissions";

/** Shareable addresses for pages: `#/<kind>/<ref>?title=…&section=…`. A link names a page; it grants nothing. Opening
 * one goes through the same page and object checks as navigating there, and an object the reader may not see stays
 * hidden. Short-lived fields (a form to open, a search, a principal to highlight) are not part of a link. */
const LINKED = ["kafkaKind", "section", "productSection", "catalogSection", "apiSection", "apiFocus", "runScope", "roleName", "groupSection"] as const;

export function toHash(t: TabRef): string {
  const params = new URLSearchParams();
  if (t.title) params.set("title", t.title);
  for (const field of LINKED) { const value = t[field]; if (value) params.set(field, String(value)); }
  if (t.productManagement) params.set("manage", "1");
  if (t.data) params.set("data", JSON.stringify({ schemaId: t.data.schemaId, table: t.data.table, path: t.data.path }));
  const query = params.toString();
  return `#/${t.kind}${t.ref ? "/" + encodeURIComponent(t.ref) : ""}${query ? "?" + query : ""}`;
}

export function fromHash(hash: string): TabRef | null {
  const match = /^#\/([a-z-]+)(?:\/([^?]*))?(?:\?(.*))?$/.exec(hash);
  if (!match || !(match[1] in TAB_PAGES)) return null;
  const params = new URLSearchParams(match[3] ?? "");
  const tab: TabRef = { kind: match[1] as TabKind, title: params.get("title") ?? match[1] };
  if (match[2]) {
    try { tab.ref = decodeURIComponent(match[2]); } catch { return null; }
  }
  const fields = tab as unknown as Record<string, unknown>;
  for (const field of LINKED) { const value = params.get(field); if (value) fields[field] = value; }
  if (params.get("manage") === "1") tab.productManagement = true;
  const data = params.get("data");
  if (data) {
    try {
      const parsed = JSON.parse(data) as { schemaId?: unknown; table?: unknown; path?: unknown };
      if (typeof parsed.schemaId === "string") tab.data = { schemaId: parsed.schemaId,
        table: typeof parsed.table === "string" ? parsed.table : undefined, path: typeof parsed.path === "string" ? parsed.path : undefined };
    } catch { /* an unreadable location: open the page without it */ }
  }
  return tab;
}

/** Pinned pages, remembered per person on this device only. A convenience: storage may be unavailable. */
const pinsKey = (user: string) => `limoon.pins.${user}`;

export function savedPins(user: string): TabRef[] {
  try { return (JSON.parse(localStorage.getItem(pinsKey(user)) ?? "[]") as string[]).map(fromHash).filter((t): t is TabRef => !!t); }
  catch { return []; }
}

export function savePins(user: string, tabs: TabRef[]): void {
  try { localStorage.setItem(pinsKey(user), JSON.stringify(tabs.map(toHash))); } catch { /* not remembered: still works */ }
}
