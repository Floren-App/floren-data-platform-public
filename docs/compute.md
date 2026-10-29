# Compute pools

A compute pool is a named execution size and concurrency budget. Programs request
a pool; FDP admits them when capacity is available and starts a container with
that pool's CPU/memory settings. A pool is not necessarily a permanently running
database or a dedicated Kubernetes node.

## Configure

Administrators create pools through the UI or
[`fdp_compute_pool`](reference/terraform/fdp_compute_pool.md). Choose CPU, memory,
available concurrency and access appropriate to the workload. Names such as
`xsmall` and `small` are examples; an empty installation must configure them.
Workspace settings may choose a pool to size editor sessions separately from jobs.

Pools apply within the placement and layer rules of the requesting object. A larger
pool never authorizes another layer's data or identity. Runtime images and Python/R
dependency versions are configured separately.

## Scope warehouses by layer and kind of work

A warehouse can be limited to layers and kinds of work (`script`, `notebook`, `app`, `dbt`, `airflow`), next to
its groups. Together they express rules such as "this warehouse is for dbt projects of the data engineers in
Prod". Empty means every layer or every kind.

```hcl
resource "fdp_compute_pool" "prod_dbt" {
  name           = "prod-dbt"
  cpu            = 8
  memory_gb      = 32
  allowed_groups = [fdp_app_group.data_engineers.id]
  layer_ids      = [fdp_layer.prod.id]
  workload_kinds = ["dbt"]
}

# dbt work in Prod that names no warehouse (or one not meant for it) runs here.
resource "fdp_default_warehouse" "prod_dbt" {
  name          = fdp_compute_pool.prod_dbt.name
  layer_id      = fdp_layer.prod.id
  workload_kind = "dbt"
}
```

- **Admission** checks the scope for every run, deployment, Airflow worker and Airflow-triggered task, and
  refuses work outside it with the reason ("is for dbt work, not app").
- **Choices** in the app only offer warehouses meant for the layer and kind at hand; `GET /v1/pools?layer_id=&kind=`
  does the same for other clients.
- **Defaults** can be set per layer, per kind or both. Work that names no warehouse, or names one in
  `fdp.json` that is not meant for it, runs on the most specific default: layer and kind, then the layer, then
  the kind, then the installation default. An explicit choice is never replaced; it is refused instead.
- **Safety**: the installation default, the workspace warehouse and Trino warehouses (other than to their own
  layers) cannot be scoped, and a warehouse cannot be narrowed while deployed workloads or defaults rely on it
  outside the new scope.

## Understand activity

A serving dashboard or Airflow deployment occupies resources while its process is
running. A callable script occupies execution resources when it actually runs.
Published registration status is different from an active process. Pool activity
should be assessed alongside running workspace, job and app executions.

Queued work waits for pool/layer capacity. A Kubernetes scheduler refusal may also
mean the cluster has insufficient CPU/memory or a layer ResourceQuota is exhausted.
Increasing application concurrency cannot create physical cluster capacity.
Inspect the waiting reason before changing limits.

## Size and observe

Start small for editors and dashboards, then measure run duration, memory and CPU.
Give long-running orchestrators enough headroom for their metadata/scheduler load.
Trino resources belong to the layer's configured query infrastructure; changing a
Python pool does not automatically resize Trino workers.

Use [query history](query-execution.md), run logs and cluster monitoring to separate
SQL bottlenecks from process startup, dependency installation or application latency.
See [troubleshooting](troubleshooting.md) for startup and scheduling checks.
