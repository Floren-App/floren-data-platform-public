# GitOps and promotion

Keep three responsibilities separate:

| Repository/configuration | Owns |
|---|---|
| Cluster infrastructure | ArgoCD applications, Helm release values, namespaces, DNS/cert-manager prerequisites |
| Platform configuration | FDP layers, groups, bindings, storage, pools and workspace templates through OpenTofu |
| Workload configuration and source | Repository registrations, publications, workload IAM, programs, dbt, Airflow and dashboards |

ArgoCD manages the FDP Helm release. OpenTofu calls the installed FDP API; it does
not replace the cluster's release management. Workload GitOps independently follows
the configured source branch. See [configuration ownership](configuration.md).

## ArgoCD installation

Use an ArgoCD Application pointing at `deploy/helm/fdp` in the source repository,
pinned to a reviewed commit or release. Store installation values in your private
cluster repository. Configure that repository as an additional values source if
using ArgoCD multi-source applications. Give ArgoCD read access to private Git
sources and Kubernetes access only where it must reconcile.

Before enabling automatic reconciliation, compare rendered objects with an
existing installation. Adoption must preserve Secret names, PVCs, namespaces,
database locations and generated installation identity. A different release name
or namespace can create a second installation rather than adopt the first.

Do not run manual Helm upgrades against an ArgoCD-owned release: commit the
desired values and let ArgoCD reconcile them. A rollback is a reviewed Git revert
plus verification, not deletion/recreation of storage.

## Workload promotion

For a Test/Prod project:

1. Bind Test workloads to the Non-Prod layer and the `test` branch.
2. Bind Prod workloads to the Prod layer and `main`.
3. Grant engineers development access in Non-Prod; give analysts only product access.
4. Validate ingestion, dbt tests, quality, lineage and dashboards in Test.
5. Review a pull request from `test` to `main`; the Prod publication follows `main`.

Branch protections and approval requirements live at the Git provider. FDP's
branch-following behavior is not itself an approval gate. Configure separate
workload identities and separate layer-local grants for each environment.
