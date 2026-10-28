import { createContext, ReactNode, useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";

type Entry<T> = { status: "idle" | "loading" | "ready" | "error"; data?: T; error?: string; at: number; version: number };
const EMPTY: Entry<never> = { status: "idle", at: 0, version: 0 };
const TTL = 5 * 60_000;

/** Session-only cache: no prefetch, timers, persisted metadata, or cross-user sharing. */
class ObjectCache {
  private entries = new Map<string, Entry<unknown>>();
  private listeners = new Set<() => void>();
  private pending = new Map<string, Promise<void>>();
  read = <T,>(key: string): Entry<T> => (this.entries.get(key) ?? EMPTY) as Entry<T>;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private put(key: string, entry: Entry<unknown>) {
    this.entries.delete(key); this.entries.set(key, entry);
    // Bound retained metadata; in-flight entries are protected from eviction.
    if (this.entries.size > 300) {
      const oldest = [...this.entries.keys()].find((k) => k !== key && !this.pending.has(k));
      if (oldest) this.entries.delete(oldest);
    }
    this.listeners.forEach((listener) => listener());
  }
  load<T>(key: string, fetcher: () => Promise<T>, force = false): Promise<void> {
    const previous = this.read<T>(key);
    if (this.pending.has(key)) return this.pending.get(key)!;
    if (!force && (previous.status === "error" || (previous.status === "ready" && Date.now() - previous.at < TTL))) return Promise.resolve();
    const version = previous.version;
    const job = Promise.resolve().then(fetcher).then((data) => {
      if (this.read(key).version === version) this.put(key, { status: "ready", data, at: Date.now(), version });
    }, (error: Error) => {
      if (this.read(key).version === version) this.put(key, { status: "error", error: error.message, at: Date.now(), version });
    }).finally(() => { if (this.pending.get(key) === job) this.pending.delete(key); });
    this.pending.set(key, job);
    this.put(key, { ...previous, status: "loading", error: undefined });
    return job;
  }
  invalidate(prefix: string) {
    for (const [key, entry] of this.entries) if (key.startsWith(prefix)) {
      this.pending.delete(key);
      this.entries.set(key, { status: "idle", at: 0, version: entry.version + 1 });
    }
    this.listeners.forEach((listener) => listener());
  }
}

const Context = createContext<ObjectCache | null>(null);
export function ObjectCacheProvider({ children }: { children: ReactNode }) {
  const [cache] = useState(() => new ObjectCache());
  return <Context.Provider value={cache}>{children}</Context.Provider>;
}
export function useObjectCache() {
  const cache = useContext(Context);
  if (!cache) throw new Error("Object cache provider is missing");
  return cache;
}
export function useCached<T>(key: string, fetcher: () => Promise<T>, active = true) {
  const cache = useObjectCache();
  const fetchRef = useRef(fetcher); fetchRef.current = fetcher;
  const entry = useSyncExternalStore(cache.subscribe, () => cache.read<T>(key));
  useEffect(() => { if (active) void cache.load(key, () => fetchRef.current()); }, [cache, key, active, entry.version]);
  return { ...entry, refresh: () => cache.load(key, () => fetchRef.current(), true) };
}
