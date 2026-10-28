import type { ReactNode } from "react";
import { IconApi } from "./Icons";

export type ProductKind = "python" | "r" | "shiny" | "streamlit" | "airflow" | "dbt" | "iceberg" | "postgres" | "groups" | "users" | "identity" | "folder" | "table" | "data" | "file";

// Original SVG artwork, kept locally with the app. No remote assets or icon CDN.
const paths: Record<ProductKind, { color: string; drawing: ReactNode }> = {
  python: { color: "#4b9bd3", drawing: <><path d="M12 3H8a3 3 0 0 0-3 3v4h8V7h4V6a3 3 0 0 0-3-3z" /><path d="M12 21h4a3 3 0 0 0 3-3v-4h-8v3H7v1a3 3 0 0 0 3 3z" stroke="#d6b344" /><path d="M5 8H4a2 2 0 0 0-2 2v4a2 2 0 0 0 2 2h4v-4h8V8h4a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-1" /><circle cx="9" cy="6" r=".6" fill="currentColor" /><circle cx="15" cy="18" r=".6" fill="#d6b344" /></> },
  r: { color: "#5794db", drawing: <><ellipse cx="11" cy="11" rx="9" ry="6" /><path d="M9 19V8h5a3 3 0 0 1 0 6H9m5 0 5 6" strokeWidth="2.5" /></> },
  shiny: { color: "#71b3e5", drawing: <><path d="m12 2 8 4v12l-8 4-8-4V6z" /><path d="m13 6-5 7h4l-1 5 6-8h-4z" fill="currentColor" stroke="none" /></> },
  streamlit: { color: "#ef7077", drawing: <><path d="m3 8 5 3 4-6 4 6 5-3-3 10H6z" /><path d="M7 14h10" /><circle cx="3" cy="6" r="1" /><circle cx="12" cy="3" r="1" /><circle cx="21" cy="6" r="1" /></> },
  airflow: { color: "#51b9a0", drawing: <><path d="M12 12V3l5 4z" fill="#4b9bd3" /><path d="M12 12h9l-4 5z" fill="#e27878" /><path d="M12 12v9l-5-4z" fill="#51b9a0" /><path d="M12 12H3l4-5z" fill="#e7c566" /><circle cx="12" cy="12" r="1.5" fill="currentColor" /></> },
  dbt: { color: "#ef987a", drawing: <><path d="m12 2 3 7 7 3-7 3-3 7-3-7-7-3 7-3z" /><path d="m8 8 8 8m0-8-8 8" /></> },
  iceberg: { color: "#65b7d6", drawing: <><path d="m3 14 6-10 4 6 3-3 5 7zM3 17h18M7 20h10M9 4l1 10m3-4 2 4" /></> },
  postgres: { color: "#6f9fc7", drawing: <><path d="M8 17c-3 1-5-1-5-5 0-6 3-9 9-9s9 3 9 9c0 3-2 5-5 4M7 8c-2 5 0 8 3 7m7-7c2 5 0 8-3 7M10 12v7a2 2 0 0 0 4 0v-7" /><path d="M14 18c4 2 6 0 5-2" /><circle cx="9" cy="9" r=".6" fill="currentColor" /><circle cx="15" cy="9" r=".6" fill="currentColor" /></> },
  groups: { color: "#a99ade", drawing: <><circle cx="9" cy="8" r="3" /><circle cx="18" cy="9" r="2" /><path d="M2 20v-2a7 7 0 0 1 14 0v2m1-6a5 5 0 0 1 5 4v2" /></> },
  users: { color: "#8ebcd5", drawing: <><circle cx="12" cy="8" r="4" /><path d="M4 21v-1a8 8 0 0 1 16 0v1" /></> },
  identity: { color: "#9ebc87", drawing: <><path d="m12 2 8 4v6c0 5-4 8-8 10-4-2-8-5-8-10V6z" /><circle cx="11" cy="11" r="2.5" /><path d="m13 13 4 4m-1-1 2-2" /></> },
  folder: { color: "#d2b36d", drawing: <path d="M3 6h7l2 2h9v12H3zM3 6V4h6l2 2h9v2" /> },
  table: { color: "#91b9c8", drawing: <><rect x="3" y="4" width="18" height="16" rx="1" /><path d="M3 9h18M3 14h18M9 9v11M15 9v11" /></> },
  file: { color: "#91b9c8", drawing: <><path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8M8 17h8" /></> },
  data: { color: "#91b9c8", drawing: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0" /></> },
};

export function productKind(kind?: string, runtime?: string): ProductKind {
  if (kind?.startsWith("dbt_")) return "dbt";
  if (kind?.startsWith("airflow_")) return "airflow";
  if (kind === "view" || kind === "materialized_view") return "table";
  if (kind === "pg") return "postgres";
  if (kind === "s3") return "folder";
  if (kind === "app") return runtime === "r" ? "shiny" : "streamlit";
  if (kind === "script" || kind === "notebook") return runtime === "r" ? "r" : "python";
  return kind && kind in paths ? kind as ProductKind : "data";
}

export function ProductIcon({ kind, runtime, size = 18 }: { kind?: string; runtime?: string; size?: number }) {
  if (kind === "api" || kind?.startsWith("api-")) {
    return <span className="product-icon" aria-hidden="true" title="api" style={{ color: "#6aa8d8" }}><IconApi size={size} /></span>;
  }
  const key = productKind(kind, runtime), art = paths[key];
  return <span className="product-icon" aria-hidden="true" title={key} style={{ color: art.color }}>
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">{art.drawing}</svg>
  </span>;
}
