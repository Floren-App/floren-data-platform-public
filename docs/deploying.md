# Publishing workloads

Develop privately in a workspace, share code through Git, and publish a program
from a registered repository. Publishing creates a governed workload with its own
resource IAM and built-in managed identity. It does not expose your workspace volume.

## Prepare a repository

Put `fdp.json` at the repository root and commit the entrypoints and dependency
files it references. An administrator registers the Git integration/repository
with a read-only technical credential for the control plane. Personal workspace Git
credentials and integration credentials serve different purposes.

In **Administration → Repositories**, confirm the clone URL, default branch and
who can publish from it. For private Git, store its credential through the secret
backend, not in a clone URL or JSON file. See [Terraform](terraform.md) for
write-only repository credential inputs.

## Publish from the UI

1. Select the active app group that should own the new workload.
2. Open **Workloads**, choose its layer/folder, and use **+ → Publish**.
3. Select the repository and branch, fetch its config and choose a program.
4. Set the publication's title/slug, compute pool and execution mode.
5. Publish, inspect validation/startup status, then configure its data/API grants
   and the users/groups allowed to run/open it.

Use **+ → Folder** for nested workload folders. Each folder has its own IAM page;
parent grants flow down and children may add grants. Security app groups are not
workload folders. The active group owns newly created objects.

## Built-in identity and data access

Prefer **fixed execution with the workload's own managed identity**. Assign that
identity roles directly on the required schemas, storage resources and APIs.
A separate service account is optional for an external/shared execution identity,
not a requirement for every publication.

A publication's execute/open grant and its identity's data grants are separate.
An analyst can open a fixed-identity reporting dashboard without receiving raw data
access. A Contributor who edits/deploys code can exercise the publication's existing
identity through that code; protect identity selection and grant assignment separately.

In caller mode, the dashboard must delegate each viewer's data requests using
viewer-local context. Do not share a privileged data connection or cache results
across viewers. See [RBAC](rbac.md).

## GitOps and promotion

With branch sync enabled, the control plane checks the configured branch for new
commits. Valid updates are deployed; a failed update keeps the last serving version
and reports an error. **Check for updates** requests an immediate check. With sync
disabled, the publication stays on its deployed commit until deliberately updated.

Scripts run when invoked; a registered script does not need a continuously running
process. Apps keep a serving process. dbt supports execution and its generated docs;
Airflow serves an orchestrator. Inspect run outcomes separately from publication status.

Use `test` and `main` branches with distinct layer-bound Test/Prod publications.
Promotion is a reviewed Git change; configure branch protection at the Git provider.
See [GitOps](gitops.md) and the [end-to-end example](pokemon.md).

## Catalog, lineage and health

Tag and describe published objects, assign accountable ownership and configure
freshness/run SLAs as appropriate. Uncertified products remain available with a
visible status. SQL, managed file/API calls and native dbt/Airflow artifacts provide
lineage evidence; declared static metadata alone cannot prove a path executed.

## Service accounts and secrets

Separate service accounts remain supported for external clients or intentionally
shared identities. Their tokens are explicit, scoped credentials: protect, expire
and revoke them independently. Security's Services view distinguishes them from
built-in workload identities and can show retired accounts for audit history.

Secrets belong to authorized user/group/service scopes and live in the configured
Kubernetes/OpenBao backend. Programs reference them by logical path, for example
`"secrets": {"API_KEY": "group:<group-id>/source-key"}`. Certificate secrets expose
certificate/key/CA paths through their supported execution environment variables.
Never place a secret value in `fdp.json`. See [secrets and certificates](secrets-and-certificates.md).

## Undeploying

Undeploying removes the callable/serving publication and stops its active serving
execution. It is different from deleting source Git or clearing a workspace.
Treat data retention and secret retirement as explicit operations; do not assume
an undeploy has erased datasets or every historical artifact.
