import { useCallback, useEffect, useRef, useState } from "react";
import { api, AccessResource, AppFeature, Layer, Me, modeLayerId, Workload, Run, Workspace } from "./api";
import { useObjectCache } from "./cache";
import { SessionExpiredError } from "./auth";
import { pageVisible } from "./permissions";

export interface Hub {
  me: Me | null;
  workspaces: Workspace[];
  /** The most recent runs: RUN_PAGE at first, growing by RUN_PAGE with loadMoreRuns. */
  runs: Run[];
  hasMoreRuns: boolean;
  loadMoreRuns: () => Promise<void>;
  workloads: Workload[];
  resources: AccessResource[];
  layers: Layer[];
  error: string | null;
  refresh: () => Promise<void>;
  selectActiveGroup: (id: string) => Promise<void>;
  /** The layer mode: lists show this layer and new work lands in it. */
  layerId: string;
  selectActiveLayer: (id: string) => Promise<void>;
  /** Usage shows every layer instead of the layer mode (sidebar and page share it; not remembered). */
  usageAllLayers: boolean;
  setUsageAllLayers: (all: boolean) => void;
  /** Whether the active group's app profile includes a feature (pages, navigation and the actions behind them). */
  has: (feature: AppFeature) => boolean;
  /** Runs an action, surfaces its error, and refreshes state afterwards. Resolves true on success. */
  act: (fn: () => Promise<unknown>) => Promise<boolean>;
  dismissError: () => void;
}

const POLL_MS = 5000;
export const RUN_PAGE = 20;
const MAX_RUNS = 500;
const IDENTITY_POLL_MS = 30_000;
const stable = <T,>(before: T, after: T): T => JSON.stringify(before) === JSON.stringify(after) ? before : after;

export function useHub(enabled: boolean): Hub {
  const cache = useObjectCache();
  const [me, setMe] = useState<Me | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [hasMoreRuns, setHasMoreRuns] = useState(false);
  // Polling refreshes every loaded run, so statuses stay current after "Load more".
  const runLimit = useRef(RUN_PAGE);
  const [workloads, setWorkloads] = useState<Workload[]>([]);
  const [layers, setLayers] = useState<Layer[]>([]);
  const [resources, setResources] = useState<AccessResource[]>([]);
  const [error, setError] = useState<string | null>(null);

  // A failed action's message must survive the next successful poll, so polling only reports
  // its own failures and clears only an error it raised itself.
  const pollFailed = useRef(false);
  const groupRevision = useRef(0);

  const inFlight = useRef<{ revision: number; full: boolean; limit: number; promise: Promise<void> } | null>(null);
  const identityUpdated = useRef(0);
  // The app profile decides which listings to load: features that are off are refused by the API.
  const features = useRef<Set<string> | null>(null);
  // A server without app profiles sends no list: then everything is on, as before.
  const featurePages: Partial<Record<AppFeature, string>> = { workspaces: "workspaces", "workloads.view": "workloads", "workloads.manage": "workloads",
    "products.browse": "products", "products.request": "requests", "products.publish": "products", "catalog.objects": "catalog", storage: "storage", packages: "packages", kafka: "kafka", apis: "apis", usage: "usage", security: "set-groups" };
  const learnedPages = useRef<string | null>(null);
  const learn = (value: Me) => {
    const revision = JSON.stringify(value.pages ?? {});
    if (learnedPages.current !== null && learnedPages.current !== revision) cache.invalidate("");
    learnedPages.current = revision;
    features.current = new Set((value.app_features ?? []).filter((f) => pageVisible(value, featurePages[f] ?? "")));
  };

  const update = useCallback(async function update(includeResources: boolean): Promise<void> {
    const revision = groupRevision.current;
    const limit = runLimit.current;
    const pending = inFlight.current;
    if (pending?.revision === revision) {
      await pending.promise;
      // Explicit refreshes and pagination must not be swallowed by an older poll.
      if ((includeResources && !pending.full) || limit > pending.limit) await update(includeResources);
      return;
    }
    const current = () => revision === groupRevision.current;
    const publish = <T,>(promise: Promise<T>, apply: (value: T) => void) => promise.then((value) => {
      if (current()) apply(value);
    });
    let identityLoaded = false;
    if (!features.current) {  // first load: learn the profile before asking for listings
      try {
        const value = await api.me();
        learn(value); identityUpdated.current = Date.now(); identityLoaded = true;
        if (current()) setMe((prev) => stable(prev, value));
      } catch { /* reported below by the identity task */ }
    }
    const has = (f: string) => !features.current || features.current.has(f);
    const tasks = [
      has("workspaces") ? publish(api.workspaces(), (value) => setWorkspaces((prev) => stable(prev, value))) : Promise.resolve(setWorkspaces([])),
      has("workspaces") || has("workloads.view") ? publish(api.runs(Math.min(limit + 1, MAX_RUNS)), (value) => {
        setRuns((prev) => stable(prev, value.slice(0, limit)));
        setHasMoreRuns(value.length > limit && limit < MAX_RUNS);
      }) : Promise.resolve(setRuns([])),
      has("workloads.view") ? publish(api.workloads(), (value) => setWorkloads((prev) => stable(prev, value))) : Promise.resolve(setWorkloads([])),
    ];
    if (!identityLoaded && (includeResources || Date.now() - identityUpdated.current >= IDENTITY_POLL_MS)) {
      tasks.push(publish(api.me(), (value) => {
        identityUpdated.current = Date.now();
        learn(value);
        setMe((prev) => stable(prev, value));
      }));
    }
    if (includeResources) tasks.push(
      publish(api.access.resources(), (value) => setResources((prev) => stable(prev, value))),
      publish(api.layers.list(), (value) => setLayers((prev) => stable(prev, value))),
    );
    const flight = { revision, full: includeResources, limit, promise: Promise.resolve() };
    flight.promise = Promise.allSettled(tasks).then((results) => {
      if (!current()) return;
      const failure = results.find((r): r is PromiseRejectedResult => r.status === "rejected" && !(r.reason instanceof SessionExpiredError));
      if (failure) {
        pollFailed.current = true;
        setError(`Cannot reach Floren Data Platform: ${(failure.reason as Error).message}`);
      } else if (pollFailed.current && results.every((r) => r.status === "fulfilled")) {
        pollFailed.current = false;
        setError(null);
      }
    }).finally(() => { if (inFlight.current === flight) inFlight.current = null; });
    inFlight.current = flight;
    await flight.promise;
  }, []);

  const refresh = useCallback(async () => {
    cache.invalidate("catalog:");
    // Mutations/explicit refreshes invalidate any older in-flight snapshot.
    groupRevision.current += 1;
    await update(true);
  }, [cache, update]);

  const act = useCallback(
    async (fn: () => Promise<unknown>) => {
      let ok = true;
      try {
        setError(null);
        pollFailed.current = false;
        await fn();
      } catch (e) {
        ok = false;
        if (!(e instanceof SessionExpiredError)) setError((e as Error).message);
      }
      await refresh();
      return ok;
    },
    [refresh],
  );

  const dismissError = useCallback(() => setError(null), []);

  const selectActiveGroup = useCallback(async (id: string) => {
    try {
      setError(null);
      pollFailed.current = false;
      await api.selectActiveGroup(id);
      groupRevision.current += 1;
      const current = await api.me();
      // Discard older polls and update the selector without waiting for runtime listings.
      identityUpdated.current = Date.now();
      learn(current);  // the new active group may bring another app profile
      setMe(current);
      setWorkloads([]);
      setResources([]);
      setRuns([]);
      setHasMoreRuns(false);
      runLimit.current = RUN_PAGE;
      cache.invalidate("");
      void update(true);
    } catch (e) {
      if (!(e instanceof SessionExpiredError)) setError((e as Error).message);
    }
  }, [cache, update]);

  const selectActiveLayer = useCallback(async (id: string) => {
    const before = me;
    setMe((m) => m && { ...m, active_layer_id: id });
    try { setError(null); await api.selectActiveLayer(id); }
    catch (e) { setMe(before); if (!(e instanceof SessionExpiredError)) setError((e as Error).message); }
  }, [me]);
  const layerId = modeLayerId(me, layers);
  const [usageAllLayers, setUsageAllLayers] = useState(false);

  const loadMoreRuns = useCallback(async () => {
    runLimit.current = Math.min(runLimit.current + RUN_PAGE, MAX_RUNS);
    await update(false);
  }, [update]);

  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async (full = false) => {
      if (full || document.visibilityState !== "hidden") await (full ? refresh() : update(false));
      if (!stopped) timer = setTimeout(() => void poll(), POLL_MS);
    };
    const visible = () => { if (document.visibilityState === "visible") void update(false); };
    void poll(true);
    document.addEventListener("visibilitychange", visible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      groupRevision.current += 1;
      document.removeEventListener("visibilitychange", visible);
    };
  }, [enabled, refresh, update]);

  return { me, workspaces, runs, hasMoreRuns, loadMoreRuns, workloads, resources, layers, error, refresh, selectActiveGroup, layerId, selectActiveLayer, usageAllLayers, setUsageAllLayers, act, dismissError,
    has: (feature: AppFeature) => !!me?.app_features?.includes(feature) && pageVisible(me, featurePages[feature] ?? "") };
}
