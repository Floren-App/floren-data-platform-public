import { lazy, Suspense, useEffect, useState } from "react";
import { api, type CatalogAsset } from "../api";
import type { TabRef } from "../views/Tabs";

const Explorer = lazy(() => import("./LineageExplorer").then((module) => ({ default: module.LineageExplorer })));

/** Storage, workloads and catalog resolve to the same asset and use the same explorer. */
export function ObjectLineage({ resourceId, objectPath, openTab }: {
  resourceId: string; objectPath?: string; openTab: (tab: TabRef) => void;
}) {
  const [asset, setAsset] = useState<CatalogAsset | null>(null), [error, setError] = useState("");
  useEffect(() => {
    let stale = false;
    setAsset(null); setError("");
    api.catalog.resourceAsset(resourceId, objectPath).then((value) => { if (!stale) setAsset(value); })
      .catch((reason: Error) => { if (!stale) setError(reason.message); });
    return () => { stale = true; };
  }, [resourceId, objectPath]);
  if (error) return <p className="error" role="alert">{error}</p>;
  if (!asset) return <p className="muted" role="status">Loading object lineage…</p>;
  return <>
    <p className="hint">This is the same object and lineage shown in the catalog. <button className="text-button" onClick={() => openTab({ kind: "catalog-object", ref: asset.id, title: asset.name, catalogSection: "lineage" })}>Open in catalog</button></p>
    <Suspense fallback={<p className="muted" role="status">Loading lineage explorer…</p>}><Explorer asset={asset} openTab={openTab} version={0} /></Suspense>
  </>;
}
