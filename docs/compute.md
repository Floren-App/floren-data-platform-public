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
