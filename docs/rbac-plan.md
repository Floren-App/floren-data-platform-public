# RBAC planning draft

Status: implementation authorized on 17 October 2026. This is the design and interview record.
See [rbac.md](rbac.md) for implemented behavior, setup, native connector coverage, and remaining work.
This document records the requested behavior, current code, proposed direction,
and decisions still needed. Proposed defaults are not confirmed requirements.

The user subsequently requested one SQL executor for PostgreSQL and Iceberg. See
[Shared SQL execution](query-execution.md) for the implemented Trino path and
delivery sequence, which supersedes the per-engine adapter expansion order below.
Trino is now the default SQL path; native PostgreSQL leases remain a legacy migration mode.

## Confirmed requirements

- Govern databases and schema objects within each data source, with schema-level grants.
- Provision users and identity roles/groups from the IdP through SCIM.
- Organize deployed programs and apps in nested groups (called folders here to distinguish
  them from identity groups), and grant access at folder level.
- Data roles: Owner, Admin, Contributor, Reader.
- Folder/program roles: Owner, Admin, Contributor, Executor.
- First release uses fixed built-in permission roles. Allow application groups and identity
  bindings; custom roles with editable permission bundles are deferred.
- Data grants are schema-level for the first release and cover all current/future objects.
  Individual object grants are deferred; object discovery remains necessary for enforcement.
- Owner can assign Floren Data Platform permissions; Admin cannot assign Floren Data Platform permissions.
  Native database grants are an accepted exception where the engine couples them to DDL:
  allow native DDL to Admin even when this also permits native grants (confirmed).
- Data Contributor reads and writes data but cannot create or alter tables.
- Folder Admin can create and remove apps/programs; Contributor can modify them;
  Executor can run them.
- Register each deployed program/app as a service, with assignable roles/groups.
- Floren Data Platform owns an application group/role layer between resource permissions and
  IdP groups, roles, and individual users. Allow creation of these application groups/roles.
- Platform admins create app groups and designate group owners. Designated group owners
  manage the group's membership and IdP bindings; this does not let them edit resource grants.
- Contributors can deploy code changes, including automatic GitOps updates, using the
  workload's already-configured identity. Identity, data grants, and permission assignments
  remain separately protected.
- Inheritance is additive: parent grants always apply; children can add grants and cannot
  block inherited access. No explicit deny or inheritance breaks.
- Support both caller-identity and fixed-identity execution, configured by the owner/deployer.
  Fixed identity choices are the workload's registered service principal or an authorized
  service account. A deployer/owner human identity is not a fixed execution option.
- In caller mode, shared apps must enforce the logged-in viewer's permissions on every
  data request through delegated access; this is required behavior, not an optional feature.
- Keep IdP support generic; choose the production provider later. Do not block the common
  model on selecting a provider or build a proprietary provisioning dependency into it.
- Revocation blocks new access, while already-running jobs finish with their existing
  permissions. Shared-app viewers' new data requests use current permissions.

## Current project

Floren Data Platform is a Tauri/React desktop hub backed by a FastAPI/SQLAlchemy control plane,
Postgres, Traefik, OpenBao, and Docker/Kubernetes workload orchestration.
Private workspaces contain code; deployed programs/apps come from Git commits and
can follow branches through the GitOps reconciler. Python, R, notebooks, Streamlit,
Shiny, dbt, and Airflow are represented in the current implementation.

Relevant code and behavior:

| Area | Current behavior | Main files |
|---|---|---|
| Human identity | OIDC claims supply username, roles, group-name strings; no persisted user/group directory or SCIM routes found | `app/auth.py`, `app/deps.py` |
| Data authorization | `DataGrant` connects an entire data source to a group-name string, with no permission level | `app/models.py`, `app/routes/admin.py`, `app/deps.py` |
| Credentials | `/v1/connect/{name}` returns source credentials after a group intersection; optional OpenBao dynamic role is configured per source | `app/routes/connect.py`, `app/secrets.py` |
| Network | Source-level pod labels allow data-source egress on Kubernetes; labels are set at workload startup | `app/executions.py`, `app/orchestrator/kubernetes.py` |
| Workloads | Flat catalog; owner/platform admin manages; `allowed_groups` controls visibility/use; empty means everyone | `app/routes/deploy.py`, `app/models.py` |
| Execution | Deployed scripts run as caller; persistent apps use deployer or selected service account | `app/routes/deploy.py`, `app/deploying.py` |
| Service accounts | Separate `groups` and `allowed_groups`; permitted use also enables token and secret management | `app/identity.py`, `app/routes/identity.py` |
| Sessions | Workload JWTs and gateway tickets/cookies carry group snapshots | `app/auth.py`, `app/executions.py`, `app/routes/gateway.py` |
| Database evolution | Startup creates tables/adds missing columns; no formal migration history | `app/main.py`, `app/db.py` |

Documentation is not fully synchronized with implementation. In particular,
`docs/architecture.md` lists app sharing as future work, while deploying and service
accounts already exist. Use code and tests as the basis for migration planning.

## Proposed common authorization model

Confirmed authorization chain, expressed from permission to person:

`Permission on resource -> App Group/Role -> IdP Group/Role/User -> User`

Proposed representation:

- `RoleDefinition`: fixed built-in action bundle for the named data and program roles.
  No custom role definitions or edits to built-in permission bundles in the first release.
- `AppGroup`: a Floren Data Platform-managed collection, such as Finance Analysts, that can receive
  scoped roles. Keep collection membership separate from permission-bundle definition.
- `RoleAssignment`: app group + role definition + resource scope. An application role can
  also have external bindings directly, without requiring an extra collection in the UI.
- `IdentityBinding`: app group/application role -> IdP group, IdP role, or IdP user.
- `ServiceBinding`: app group/application role -> registered service principal, so machines
  traverse the same application-owned layer without pretending to be IdP human users.
- `Resource`: data source/database/schema/object, program folder, or deployed service.

External identities do not receive resource permissions directly. An individual IdP user
may be bound into the app layer, as requested. External role names never automatically
become application permission bundles. Example: SELECT on finance.reporting is supplied
by the data Reader role assigned there to Finance Analysts; IdP group Finance-Europe is
bound to Finance Analysts; Ana receives access through her current IdP membership.

The proposed group/role representation is a design choice, not a requirement for extra
user-facing concepts. The UI should make the requested chain easy to inspect and explain.
Editing shared app-group membership or identity bindings can expand access across many
resources. Platform admins create groups and designate group owners; those owners manage
membership/bindings. Resource Admin alone does not grant that group-management authority.
Built-in role definitions remain fixed. Application-group ownership, resource Owner, and
Git/workload creator are separate relationships.

Group owners may change who receives a group's existing resource permissions; this is
explicitly delegated authority over access across those resources. They cannot assign new
resource roles to their group, edit permission bundles, or alter IdP-managed membership at
the source. Binding an IdP group selects it for app-group membership; its underlying user
membership remains IdP-authoritative. Audit before/after bindings and the affected scopes.
Proposed default: only platform admins change designated group owners or delete app groups.

Use stable internal IDs and identity-provider IDs, not mutable usernames or group names,
for memberships, resource ownership, and grants. Bind OIDC issuer/subject to provisioned
users through a defined IdP mapping; never assume SCIM externalId always equals OIDC sub.
Maintain an authoritative active/disabled state and membership graph.

One authorization component should answer `authorize(subject, action, resource)` and
explain access, including the granting group and ancestor resource. API routes,
gateway admission, credential issuance, deployment/reconciliation, and orchestration
must use the same policy model.

Confirmed inheritance and proposed supporting policy:

- Deny when there is no grant; use explicit authenticated-everyone grants for broad access.
- Inherit down resource trees; combine allowed actions from applicable grants.
- No explicit deny and no inheritance breaks (confirmed).
- Keep creator metadata separate from authorization; creation establishes an explicit
  initial Owner grant under a defined ownership policy.
- Check folder moves at source and destination and show resulting access changes.
- Prevent resource/group membership cycles. Resource folders and identity groups are
  independent hierarchies.
- Define platform administration/recovery separately from resource Owner, including
  whether it bypasses data access and how that is audited.
- Treat membership administration as a privileged action: adding oneself to an Owner
  group is permission escalation even without editing a resource grant.

## Data permissions

Proposed resource tree: data source -> database/catalog -> schema/namespace -> objects.
Adapt the hierarchy to each connector; S3 bucket/prefix resources need a separate mapping.
Schema grants cover existing and future objects. Individual table/view/object grants are
deferred. Inventory objects and apply schema policy to them without exposing individual
object assignment APIs in the first release. Parent scopes must not accidentally confer
database/server administration as a consequence of granting schema access.

| Action | Owner | Admin | Contributor | Reader |
|---|---|---|---|---|
| Read data | Yes | Yes | Yes | Yes |
| Insert/update/delete rows | Yes | Yes | Yes | No |
| Create/alter/drop tables and other supported objects | Yes | Yes | No | No |
| Assign/revoke Floren Data Platform resource roles | Yes | No | No | No |
| Native database grants | Connector-dependent | Allowed if coupled to DDL | No | No |

This matrix interprets Contributor as prohibiting all DDL, including DROP. Confirm
TRUNCATE, stored procedures, functions, views, sequences, schema/database creation,
and schema/database deletion explicitly rather than treating them as generic writes.
Reader also needs only the metadata and namespace usage required to read authorized data.

Enforcement must occur at the database/catalog/storage boundary. Existing shared
credentials cannot distinguish Reader from Contributor. SDK validation alone is insufficient
because workspaces execute arbitrary Python/R and can connect directly.

Design connector adapters for discovery, privilege reconciliation, credential issuance,
revocation, and reporting actual enforcement state. Limit credentials to the effective
resource/action set. Never silently issue a more privileged credential if provisioning fails.
Model desired policy separately from applied policy so the UI can report pending/failed
changes and detected native-permission drift.

Confirmed native DDL policy: PostgreSQL ties ALTER/DROP rights to object ownership,
which also permits grants. Allow Admin the native authority necessary for DDL, including
native grant capability where inseparable. A controlled DDL/deployment service is not
required to preserve an absolute grant prohibition. Keep Floren Data Platform role-assignment APIs
Owner-only; never map native object ownership back to application Owner automatically.
Keep privileges scoped to the managed schema/object set rather than granting server-wide
administration. Where native DDL and grant authority can be separated, continue separating them.

Report the native exception as part of the connector's effective capabilities. Native
grants may bypass the app-owned role layer and create persistent access outside it;
inventory and surface such grants as drift. Decide whether reconciliation reports or removes
out-of-band grants and how native grants are cleaned up after an Admin loses their role.
Floren Data Platform's assignment boundary is strict in its own APIs; an absolute native database
assignment boundary is explicitly not promised for these Admin connections.

Iceberg authorization must include catalog operations and object-store access; broad
S3 keys can bypass catalog restrictions. S3 files need prefix/object policies. The existing
source-level network boundary remains useful but cannot enforce schema/table permissions.

## Program folders and registered services

Proposed resource tree: deployed root -> folder -> nested folders -> program/app service.
A workload retains its ID and stable URL when moved; its authorization ancestry changes.
Represent one registered service identity per deployed item, including runnable programs,
apps, dbt workloads, and Airflow deployments as applicable.

| Action | Owner | Admin | Contributor | Executor |
|---|---|---|---|---|
| Discover/open/run authorized deployed item | Yes | Yes | Yes | Yes |
| Modify program/app | Yes | Yes | Yes | No |
| Deploy changes / enable configured GitOps deployment | Yes | Yes | Yes | No |
| Create/remove programs/apps in folder | Yes | Yes | No | No |
| Assign/revoke resource roles | Yes | No | No | No |

Contributors may deploy code changes using the existing execution identity, including
automatic GitOps updates. This does not authorize changing execution identity, data grants,
or permission assignments. Confirm folder creation/removal/moves and production environment/
secret-reference management separately; deploying a new item also requires create rights.
Existing Git authorization continues to control repository writes. Floren Data Platform RBAC controls
workload actions; it cannot prevent a Git committer from changing an auto-deployed branch.
Align branch protections and deploying policy with Contributor authority. Do not require
Owner/Admin approval for every Contributor deployment: the user explicitly allows Contributor
deployments. The reconciler uses the workload's configured deployment authority, validates
the current service/data policy, and must reject manifests that attempt to change protected
identity or grant settings. Explain that repository writers can affect auto-deployed code.

Keep service-as-resource separate from service-as-principal:

- Grants on the service determine who may manage or invoke it.
- Grants to its principal determine its data access and which other services it may invoke.
- Running a service must not automatically permit impersonation, minting its API tokens,
  managing its secrets, changing its group membership, or changing its data grants.
- Editing executable code can exercise the service's data authority. Make that consequence
  explicit when defining Contributor access and deployment policy.

Both execution modes are required, configured by the owner/deployer:

- Caller mode: each invocation uses the initiating principal's current effective app roles.
- Fixed-identity mode: use the workload's registered service principal (recommended
  default for production) or another explicitly authorized service account. No fixed human
  deployer/owner identity option. Existing workloads that run as their deployer need
  an explicit migration to caller mode or an approved service identity; do not silently copy
  a person's full data access to an automatically created service principal.

Do not let execution mode be overridden by an Executor in the run request. Record the
initiator, execution principal, service, workload, and deployed commit separately.
Changing execution identity is separately protected from Contributor deployment authority:
it requires the authorized owner/deployer configuration action and permission to bind the
selected service identity. Proposed default: resource Owner configures mode/identity, with
the initial deployer allowed to select an authorized identity during creation. Creator
metadata alone never remains a permanent authorization bypass. Changing the service's
data privileges still requires Owner on the affected data resource.
An authorized service-account binding permits execution by the workload without granting
the deployer or its users permission to mint account tokens or read account secrets.

For shared persistent apps, caller mode requires a delegated data-access architecture.
Keep the app container's infrastructure identity separate from viewer-bound data requests.
Bind authenticated viewer context to data access using verified, short-lived delegation;
never accept a user-supplied username or mutable process-wide credential as identity.
Partition cached results by effective authorization context, and prevent fixed app credentials
from supplying an alternative access path in caller mode. This is a distinct implementation
phase from caller-mode batch jobs, but is confirmed scope. Do not label a shared app as
supporting caller mode until viewer-bound data access works end to end.

## SCIM and identity lifecycle

Implement SCIM Users/Groups, including discovery, provider-required filtering/pagination,
create/update/PATCH, membership changes, disable/delete semantics, and authenticated
provisioning credentials. Keep the protocol implementation provider-neutral; exercise the
standard contract with conformance fixtures, then add provider interoperability tests once
the production provider is chosen. Local Keycloak remains the existing OIDC development setup,
not proof of SCIM interoperability or a required production provider.
SCIM role attributes do not by themselves define Floren Data Platform permission bundles or a
portable role-definition provisioning API.

IdP-managed identities remain authoritative in the IdP. Application groups/roles are
managed in Floren Data Platform and carry explicit provenance and administration permissions.
Creating groups back in the IdP is outside the current requirement. Confirm nested
identity-group and role-attribute provisioning support in the selected IdP. SCIM supplies
identity/membership facts; Floren Data Platform defines how those facts bind to app roles.

Interactive authorization and admission of new work resolve current active state and
memberships rather than trusting embedded group snapshots. Already-running jobs are the
explicit exception described below. Define a measurable propagation target after the control
plane processes a change; SCIM delivery latency and existing native sessions also matter.

## Revocation and running jobs

Confirmed policy: block new access but allow running jobs to finish with their existing
permissions. Proposed implementation semantics:

- Authorize a job when it starts running; a queued job, retry, next schedule occurrence,
  redeployment, or separately launched child job requires current authorization. Queueing
  or holding an old token does not reserve access.
- Record the effective execution principal, app-role assignments, schema/action scope,
  and policy version used for the running job. Snapshot permission rules at the schema
  scope rather than freezing a list of tables: the confirmed rule covers future objects too.
- Bind retained authority to that run ID and its isolated workload identity. Prevent reuse
  of its credential/token to create fresh jobs or gain additional scopes. Never implement
  this by leaving a user's general account or service API token fully authorized.
- Credential renewal for that run may retain its original permissions until completion;
  it must never grow them. Reconcile native permissions without removing privileges still
  held by eligible running jobs. Use isolated runtime identities/leases so shared-role
  revocation does not accidentally terminate their access.
- On completion or stop, stop renewal and clean up run credentials and retained privileges.
  Define bounded run/lease lifetimes and connector-specific behavior during detailed design.
- Interactive workspace requests and shared-app viewer data requests are new access, not
  indefinitely grandfathered jobs. An open editor, app container, browser session, or
  WebSocket does not preserve removed permissions for future data requests.
- Background jobs submitted by an app follow normal job admission; launching another job
  requires current authority even if the parent process began before revocation.
- In fixed-identity mode, data authority belongs to the configured execution principal.
  Revoking a viewer's invoke permission blocks their future invocations; it does not revoke
  the service principal's independent grants or stop its already-running jobs.

The current direct-credential model cannot enforce a clean new-access boundary for an
interactive client that retains a broad reusable credential. Plan session tracking,
credential/session cleanup or short leases, and connector enforcement accordingly. State
the actual propagation/lease window rather than promising instant native revocation.
Hard deletion/disable handling must preserve only the explicitly authorized running-job
exception; keep identity records/tombstones long enough to audit and clean up those runs.

## Suggested implementation sequence after planning approval

1. Finalize remaining role actions, execution-identity choices, supported connector scope,
   and revocation semantics. Produce an engine enforcement matrix using generic OIDC/SCIM
   contracts; selecting a production IdP is deferred by user decision.
2. Establish migrations, stable principal/group IDs, resource records, memberships,
   role definitions, role assignments, and a common policy evaluator with access explanations.
3. Add SCIM provisioning and resolve authenticated users to the directory. Cover deactivation,
   rename, membership removal, and policy/cache invalidation.
4. Prove schema enforcement on the first selected connector through direct SQL, including
   the Floren Data Platform Admin/Owner distinction, accepted native grant exception, and permissions
   for future objects. Complete this vertical
   slice before extending the same promise to other data engines.
5. Add nested deployed folders, automatic service registration, item/folder grants, and
   enforcement for catalog, gateway, deploying, GitOps, orchestration, and invocation.
6. Add policy management and effective-access UI; migrate old data grants, workload
   visibility, service accounts, and creator ownership with an explicit access review.

Legacy grants contain no permission level, so a safe migration cannot infer Reader,
Contributor, or Admin from the old data grant. Inventory actual native credentials and
get a mapping decision. Existing empty workload visibility lists mean everyone; convert
that behavior deliberately rather than accidentally making old workloads private/public.
Do not run a permissive legacy fallback alongside the new model after cutover.

Acceptance evidence should include direct database denial of Reader writes and Contributor
DDL; Admin denial of Floren Data Platform role assignment; documented native Admin grant capabilities;
Owner success; correct nested inheritance and moves;
service-to-service invocation; no token/secret access from Executor rights; SCIM deactivation
with existing sessions; grant changes for future objects; GitOps honoring current policy;
failure to issue elevated credentials when native policy synchronization fails; and shared-app
viewer isolation across data requests, result caches, reruns, and concurrent viewers.
Also verify that a running job can continue and renew within its original scope after
revocation while a new invocation, retry, child job, and interactive request are denied;
run completion must remove its retained access. Verify schema grants on objects created
after the grant and that first-release APIs reject individual-object/custom-role assignments.
Verify that only platform admins create groups; only designated group owners/platform admins
manage their bindings; group owners cannot add resource grants through membership APIs;
Contributors can deploy but cannot alter identity/grants; fixed human execution identities
are rejected; and workload execution rights do not imply service-account token management.

## Remaining design details (proposals, not confirmed requirements)

The central product decisions are recorded above. The following details still require
review during detailed planning; they do not reopen confirmed decisions:

- Folder lifecycle: propose Owner/Admin may create child folders; moves require source
  removal and destination creation rights, with a preview of changed inherited access.
- Workload configuration: propose Owner selects execution mode/identity; Contributor
  deploys with that identity. Define which production env/secret-reference settings are
  editable and ensure secret references do not become a credential-extraction API.
- Group lifecycle: propose platform admins designate/replace group owners and delete groups;
  designated owners manage members/bindings. Define last-owner protection and transfer.
- Native DDL details: specify views, routines, sequences, TRUNCATE, schema lifecycle, and
  native out-of-band grant reporting/reconciliation. The accepted Admin exception stands.
- Platform recovery: define whether platform admins have ordinary data access or use an
  explicit audited recovery action to establish resource ownership.
- Revocation timing: select a measurable propagation target, lease/session behavior, and
  run lifetime limits consistent with allowing already-running jobs to finish.
- Legacy access migration: inventory existing credential privileges and choose initial roles;
  migrate deployer-human execution identities explicitly.
- Production IdP selection and individual object/custom permission roles remain deferred.

## Connector inventory and recommended enforcement

Repository inspection distinguishes accepted source kinds from complete integrations:

| Kind | Existing integration | Recommended enforcement |
|---|---|---|
| PostgreSQL | Python/R clients, dbt mapping, default dbt adapter, seeded warehouse | Native schema/object grants and scoped ownership authority for Admin DDL; accepted native grant capability; isolated leased credentials |
| MySQL | Python/R clients | Native database/table roles and explicit DML/DDL privileges without GRANT OPTION; MySQL database maps to schema; validate views/routines and escalation paths |
| SQL Server (`mssql`) | Python/R clients | Custom database roles and schema permissions; avoid broad db_owner/CONTROL grants; validate ownership chains and DDL interactions |
| S3 | Python boto3/files, R signed requests, seeded RustFS | Bucket/prefix/object IAM policy with scoped temporary credentials; separate object writes from IAM/bucket-policy administration |
| Iceberg | Trino/dbt integration, seeded REST fixture/RustFS | Authorization-capable REST catalog plus scoped storage credentials or server-side data access; validate commits cannot alter schemas for Contributor |
| Snowflake | Accepted kind and dbt profile mapping; no built-in Python/R connect helper; adapter not in default dbt image | Native roles, schema/table grants and managed-access schemas where suitable; validate ownership/DDL versus grant separation |
| Trino | Accepted kind and dbt profile mapping; no built-in Python/R connect helper; adapter not in default dbt image | Authenticate each effective principal and enforce catalog/schema/table actions in server access control; forbid direct backend credentials that bypass it |

Recommendation: Floren Data Platform owns application policy and reconciles a dedicated managed
portion of native privileges. Keep provisioning credentials in OpenBao, separate from
execution credentials. Do not claim control over a source while untracked PUBLIC grants,
native memberships, superuser privileges, or raw storage access bypass the intended boundary.
Offer existing-role mapping as an explicit integration mode where operators retain native
administration; its enforcement status must show whether actual privileges were verified.

Recommended delivery order (advice delegated by the user; not an implementation start):

1. Common policy/directory/folder/service foundation and PostgreSQL end-to-end enforcement.
   PostgreSQL is already in the sample deployment and exercises native DDL/ownership mapping
   with the accepted native-grant exception; no controlled DDL service is a prerequisite.
2. S3/RustFS scoped file access and Iceberg namespace/table access as the next governed
   data slice, because both are already seeded and used by the examples. In parallel product
   sequencing, deliver folder RBAC using the same policy foundation, without waiting for
   every connector to be complete.
3. MySQL and SQL Server, whose connection helpers exist today.
4. Snowflake and Trino: add native policy adapters and close client/runtime integration gaps.

S3 has no table/schema DDL: Contributor means read/write/delete objects within the authorized
prefix, while Admin can additionally manage the permitted logical namespace/resource lifecycle;
only Owner can assign Floren Data Platform permissions. Resource creation/deletion needs explicit mapping
to bucket/prefix operations rather than mechanically copying SQL permissions.

Iceberg needs particular care. The development stack uses `apache/iceberg-rest-fixture:1.10.0`
and gives workloads root RustFS keys. Replace that authorization arrangement for governed
deployments; evaluate Apache Polaris as an initial catalog candidate, retaining REST compatibility.
Catalog permissions for write/commit operations must be checked against Contributor's ban on
schema alteration, not inferred from a generic write-data privilege. Broad object write credentials
can also corrupt or replace table files; a server-side data operation path may be needed for
the exact guarantees under arbitrary workload code. Current dbt materializations drop/create
Iceberg tables, so dbt builds need Admin/Owner-equivalent DDL authority, not Contributor.

Scoped temporary storage credentials require SDK changes: Python boto3, R signing,
PyIceberg properties, and generated dbt profiles must propagate session tokens and
expiration correctly. Existing S3 helpers currently pass access key/secret without session
tokens. Verify RustFS IAM/STS features against the pinned deployment version; current online
documentation alone is not proof of behavior in image `1.0.2`.

Use native DDL for PostgreSQL/dbt under the confirmed Admin policy. Keep native authority
within the granted resource scope and document ownership, future-object, and revocation
behavior. The previously proposed controlled DDL service is no longer part of the baseline
plan. Reader/Contributor restrictions still require native enforcement; the Admin exception
does not expand either lower role's permissions.

## Standards and enforcement references

- SCIM core schema: https://datatracker.ietf.org/doc/html/rfc7643
- SCIM protocol: https://datatracker.ietf.org/doc/html/rfc7644
- PostgreSQL privilege model: https://www.postgresql.org/docs/current/ddl-priv.html
- MySQL privilege model: https://dev.mysql.com/doc/refman/8.0/en/privileges-provided.html
- SQL Server permissions: https://learn.microsoft.com/en-us/sql/relational-databases/security/permissions-database-engine
- Snowflake access control: https://docs.snowflake.com/en/user-guide/security-access-control-overview
- Trino server access control: https://trino.io/docs/current/security/file-system-access-control.html
- S3 authorization: https://docs.aws.amazon.com/AmazonS3/latest/userguide/security_iam_service-with-iam.html
- RustFS IAM/STS (version validation needed): https://docs.rustfs.com/en/security-compliance/iam
- Polaris privilege model (candidate, not yet selected): https://polaris.apache.org/releases/1.5.0/managing-security/access-control/
