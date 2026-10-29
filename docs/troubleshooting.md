# Troubleshooting

Start with the affected object and its layer. A healthy control plane does not
prove a workspace, Trino or dashboard is ready. Keep credentials and signed
gateway URLs out of pasted diagnostics.

## Workspace remains Starting

1. Read its startup/waiting message in FDP.
2. Inspect the workspace pod and events in that layer namespace.
3. Check PVC binding, scheduler capacity and image pull failures.
4. Read editor logs, or the `prepare-home` initializer logs for storage failures.

```sh
kubectl -n <layer-namespace> get pod ws-<workspace-id>
kubectl -n <layer-namespace> describe pod ws-<workspace-id>
kubectl -n <layer-namespace> logs ws-<workspace-id> -c prepare-home
kubectl -n <layer-namespace> logs ws-<workspace-id> -c main --tail=100
```

The new home initializer refuses old-layout volumes rather than deleting data.
Follow the [workspace storage upgrade guidance](workspaces.md#existing-installations-and-storage-upgrades).
Image pulls and new volume provisioning can take longer than a cached restart.

## fdp.json is not found

Place it in `/workspace` or a direct repository subfolder, not the volume root or
`/workspace/workspace`. Ensure valid JSON and supported configuration fields.
The config API returns validation errors separately from missing files. Use
Reload in the VS Code runner after repository-level edits. A stopped workspace
uses its last cached configuration; live reads require a running editor.

## Permission denied / wrong objects shown

Check the active app group, identity binding and layer before changing grants.
Opening a workload and its data requests use different permissions. On fixed-mode
workloads, inspect the workload's managed identity and assign the minimum required
role on the target schema/API. Do not solve a Non-Prod denial by granting Prod access.
See [RBAC](rbac.md) and [active groups](active-groups.md).

## Managed storage requires platform context

Use the built-in managed identity inside an FDP workspace or execution. A local
script does not automatically inherit that context. Check `FDP_API` and the
presence of a valid execution credential without printing the credential itself.
An S3 endpoint alone is not an authorized managed-storage session.

## Streamlit shows a failed JavaScript chunk

Try a hard refresh or a fresh tab to distinguish a stale frontend asset reference
from an unavailable publication. Check the app's status, deployed revision and
gateway response before restarting it. Record the failed asset path and HTTP
status; browser errors alone do not identify whether the cause is caching or routing.

## Catalog or query feels slow

Separate browser/API latency, metadata database latency and layer Trino execution.
Check control-plane request metrics, query history, database health and cluster
CPU/memory pressure. Do not fetch every schema/table simply to draw a sidebar:
children are intentionally queried and cached when an object is opened.

## dbt / Airflow failure

Open the actual run, task logs and dbt test results. Confirm the deployed Git
revision, layer, managed identity and schema grants. `dbt build` can fail because
of tests even when models materialize successfully. Airflow deployment readiness
and DAG-run success are separate states. See [dbt and Airflow](dbt-and-airflow.md).

## Terraform provider cannot be installed

Check that the provider is listed in your registry, or use a
[release download or local mirror](terraform.md#install-the-provider).
Inspect `TF_CLI_CONFIG_FILE`, the target OS/architecture and the exact version.
Do not remove state or run a destroy to fix an installation problem.
