/** Technical object types stay distinct from governance and product designations. */
export function objectKindLabel(kind: string, sourceKind?: string): string {
  const labels: Record<string, string> = {
    source: "Storage connection", schema: sourceKind === "s3" ? "Storage namespace" : "Schema",
    dbt: "dbt project", dbt_model: "dbt model", dbt_source: "dbt source reference",
    dbt_seed: "dbt seed", dbt_snapshot: "dbt snapshot", dbt_test: "dbt test",
    app: "App", script: "Program", notebook: "Notebook", airflow: "Airflow deployment",
    table: "Table", view: "View", materialized_view: "Materialized view", file: "File", folder: "Folder",
  };
  return labels[kind] || kind.replaceAll("_", " ");
}
