# Scoped permissions and Terraform

FDP has an explicit capability registry, scoped permission assignments, custom action roles,
and Reader gates for all desktop pages. These are separate from the existing data/workload
resource roles during migration. The registry also contains planned operations, including
separate drafts and exports that have no dedicated API. A registry entry alone does not
create such an operation. Use the route inventory and source-reference column to review
the actual implementation; the matrix is not an independent security certification.

The [full matrix](permission-matrix.md) shows role columns and named-check coverage.
Download its [CSV](permission-matrix.csv) for filtering. The [design](permission-model-plan.md)
records the intended final contract; [existing resource RBAC](rbac.md) describes the domain
roles that remain in use for workloads, data, APIs, packages and products.

## What is enforced in this revision

| Area | Behavior |
|---|---|
| Named grants | Default-deny action checks on a target, globally, in one layer, or explicitly all layers. Known resource layers override caller-supplied scope. |
| Roles | Viewer, Reader, Contributor, Executor, DataReader, DataContributor, AccessAdministrator, Admin and Owner bundles from the registry; an explicit action set or custom role can replace a built-in bundle. |
| Page visibility | All desktop tab kinds and aliases require their page gate. Hidden pages do not mount their content components. Navigation and open tab labels are filtered; page changes invalidate the object cache. |
| Administration | Named checks cover the administration handlers identified in the matrix, including form drafting/publication, classification changes, layer settings/infrastructure, pools, runtime registration and log configuration. |
| Privacy reads | Forms and drafts, policy inventory and review-duty inventory require explicit page/capability access. The `compliance_admin` role no longer grants access to Privacy administration pages. |
| Network allowlist | Per-layer host rules are bounded by a distinct global host ceiling. Changing the global ceiling requires its protected action. New proxy connections require signed workspace tickets and current layer policy. |
| Delegation | A scoped Owner may delegate only its capability namespace, targets, layers and held actions. An explicit AccessAdministrator may administer access in its scope without first acquiring data/configuration authority. Changing a shared custom role checks every existing assignment scope and every new action against the editor's delegation authority. |
| Administrator context | Global Admin privileges apply with Global Admin active. Selecting another group also restricts backend administrator checks; this is no longer a UI-only preview. |
| Object data | Table/column and file/directory targets; governed Trino column checks, narrowed S3 sessions, product grants limited to included objects. See [object permissions](data-object-permissions.md). |
| Workload functions | Separate named checks for settings fields, identity/mode/secret changes, deployment, sync, execution and app/docs opening; the gateway rechecks opening permission. Existing resource roles remain compatibility grants. |
| Package downloads | Explicit package or repository grants; Reader metadata access does not permit native Python/R downloads; Data Reader does. |
| Recovery | Independent control-plane, PostgreSQL, S3 and Iceberg v1/v2 backup plans, vault-referenced destinations, local-time schedules, scoped retention, offline restore and quarantine; see [platform recovery](platform-recovery.md). |
| Query log | Explicit global, layer, or own-principal read scope. SQL text needs separate permission; a page gate alone is insufficient. |

Existing settings-resource roles remain explicit compatibility grants, filtered through
the per-action role matrix. Remove broad inherited assignments when converting a recipient to granular permissions. The compatibility grant is not implicitly converted into layer authority.
Existing workload/data/API/package/product roles continue to apply at their existing boundaries.

## Role meanings

| Role | Meaning |
|---|---|
| Viewer | Data products only: discover them in the catalog and request access. |
| Reader | See the object or page and its configuration; no data, no preview, no access from code. |
| Contributor | Reader plus changing settings, configuration and metadata (drafting, publishing, applying, deploying, DDL). |
| Executor | Reader plus triggering on-demand work: runs, workspace start/stop, execution as a service identity. |
| DataReader | Reader plus reading data: SELECT, preview, files, Kafka subscribe, API reads, opening apps, outputs and logs. |
| DataContributor | DataReader plus changing data: writes, Kafka publish, API writes, publishing packages. |
| AccessAdministrator | Giving and removing access in its scope, without data or configuration. |
| Admin | Everything above plus the listed administrative/destructive actions, without delegation. |
| Owner | Admin plus delegation and listed protected controls, within the target scope. |

Contributor, Executor and DataReader do not include one another. A group can hold several roles on
one object, or use Admin or a custom action set where it needs several powers. The matrix is the
precise bundle definition. Personal ownership, reviewer duty, mandatory data policies, and
layer isolation remain additional constraints. Stored secret values are never Reader metadata.

## Assignment fields

Use `fdp_permission_assignment`:

| Field | Meaning |
|---|---|
| `target_id` | Stable target/collection ID. For page gates, use the page ID listed below. |
| `namespace` | Capability domain, such as `pages`, `compliance.forms` or `network.editor_allowlist`. |
| `layer_id` | Omit for global scope, use a layer ID for one layer, or explicitly `"*"` for all layers. Only active Global Admin can delegate all-layer scope. |
| `app_group_id` / `principal_id` | Exactly one recipient. Disabled principals cannot receive new grants. |
| `role` / `actions` / `custom_role_id` | Exactly one built-in role, explicit action set, or custom-role reference. |

Global is not an implicit layer wildcard. Resource ancestry is additive within matching scopes;
a weaker child grant does not cancel a parent. Interactive group-derived named grants use the
active group; direct principal grants remain explicit personal grants. Service contexts remain
bound to their execution layer. Revocation applies to subsequent authorization checks.

Import an assignment by its API ID. Terraform reads actual server state and reports changes;
destroy revokes that assignment. Import custom roles by ID too. A role still in use cannot be deleted.

## Engineer manages Dev network rules

A platform access owner grants the engineer group the page gate and Dev capability:

```hcl
resource "fdp_permission_assignment" "engineer_network_page" {
  target_id    = "set-network"
  namespace    = "pages"
  app_group_id = var.engineer_group_id
  role         = "Reader"
}

resource "fdp_permission_assignment" "engineer_dev_network" {
  target_id    = "set-network"
  namespace    = "network.editor_allowlist"
  layer_id     = var.dev_layer_id
  app_group_id = var.engineer_group_id
  role         = "Contributor"
}
```

The platform network owner configures the maximum permitted hosts:

```hcl
resource "fdp_egress_allowlist" "platform_ceiling" {
  hosts = {
    "github.com"              = "Git hosting"
    "*.githubusercontent.com" = "Git content"
  }
}
```

The delegated engineer can then configure Dev within that ceiling:

```hcl
resource "fdp_egress_allowlist" "dev" {
  layer_id = var.dev_layer_id
  hosts = {
    "github.com" = "Dev Git access"
  }
}
```

This grants no Prod/Test policy rights and no authority to change the platform ceiling.
An operator managing Prod and Test receives two layer assignments. A single-action assignment
can delegate only `network.editor_allowlist.read`, `apply`, or `delete`; replacing a list that
removes entries requires both `apply` and `delete`.

The allowlist governs editor HTTPS egress. Generated Kubernetes isolation policies, source egress,
external API destinations and cluster/CNI administration remain different controls; this feature
does not implement arbitrary editable Kubernetes NetworkPolicy objects. See [layer isolation](architecture/layer-isolation.md).

## Privacy form editing and publication

Give a group page Reader on `set-forms`, then an assignment in `compliance.forms` on target
`set-forms`, globally. Contributor drafts and publishes forms. To delegate only publication:

```hcl
resource "fdp_permission_assignment" "publish_forms" {
  target_id    = "set-forms"
  namespace    = "compliance.forms"
  app_group_id = var.privacy_publisher_group_id
  actions      = ["compliance.forms.publish"]
}
```

This does not grant Layers, Network, form deletion, or reviewer appointment. An explicit page
Reader grant is still required. Form permissions are currently checked on the form collection;
individual form-resource enforcement from the design remains migration work.

`fdp_custom_role` declares a named action set. Assign it using `custom_role_id`; all actions must
fit the assignment's declared namespace. Updating a shared role changes its recipients immediately
and is audited. Assign explicit actions instead when delegating narrowly without shared definitions.

## Page IDs

Administration IDs remain `set-layers`, `set-pools`, `set-runtimes`, `set-template`, `set-alerts`,
`set-profiles`, `set-network`, `set-classes`, `set-policies`, `set-forms`, `set-duties`, `set-groups`,
`set-services`, `set-register`, `set-auditlog` and `set-querylog`.

Other page IDs are `home`, `catalog`, `products`, `requests`, `people`, `workspaces`, `runs`,
`storage`, `kafka`, `usage`, `workloads`, `apis`, `reviews`, `memberships`, `cases`, `repos`,
`secrets`, `docs`, `git-creds`, `packages`, `imports` and `deadlines`.

The [page coverage table](permission-matrix.md#page-visibility-coverage) maps all detail pages
and legacy aliases to these gates. Page Owner delegates that page only, not every underlying object.

## Network migration and operations

Before enabling this revision, configure a global ceiling and explicit layer host lists.
An absent layer list denies editor internet access even when the global ceiling lists hosts.
Restart existing editors after rollout so their proxy URL contains the new signed workspace ticket.
Old unticketed CONNECT requests are denied. Tickets last 30 days and require the workspace and its
directory principal to remain active; restart a continuously running editor before expiry to renew it.
Do not copy proxy URLs into logs, Terraform outputs or support reports.

The proxy also authenticates to the control plane with `FDP_EGRESS_PROXY_TOKEN` and reports the
TCP peer address. The control plane verifies it against the workspace's current orchestrator IPs;
a Dev ticket copied into a Prod pod is refused. Bootstrap generates this separate token on upgrade;
installations managing their own platform Secret must supply it. The cluster network must preserve
workspace source IPs on proxy connections; unverifiable sources are denied.

Each new CONNECT rechecks the live policy. Existing established tunnels are not terminated by a
host-list change. Control-plane authorization failure denies new connections. Package-index and
private-address restrictions still apply in addition to both host lists.

## Remaining migration work

The registered matrix is broader than this revision's named endpoint enforcement. Artifact-specific grants; complete per-function migration for workload/data/API/package/product
roles; per-control capability wiring everywhere; workflow/draft additions; and complete non-HTTP/backend
conformance remain unfinished. Legacy resource grants continue to govern those areas. The named-check
column is generated from source to make this gap visible. Do not deploy on the assumption that all
506 actions can already be independently delegated across every backend.

## Private workspace functions

Workspace navigation needs `pages` Reader on `workspaces`. Function grants use
namespace `workspaces`, collection target `workspaces`, and an explicit layer.
Individual grants use `workspace:<id>`; the server derives the layer from that
workspace and rejects a conflicting layer. Grants never share another person's
private files.

| Role | Workspace functions |
|---|---|
| Reader | List and inspect own workspace metadata; no file contents |
| Contributor | Create workspaces and edit ordinary settings; no execution or files |
| Executor | Start and stop; opening an executable editor also requires DataContributor |
| DataReader | Read and download files |
| DataContributor | Change files; opening an executable editor also requires Executor |
| Admin | All listed operational functions including deletion |
| Owner | Administrative functions and permission delegation |

Job submission additionally requires `runs.create` in namespace `runs` on the workspace
or collection target, scoped to its layer. A workspace Contributor grant alone does not
submit jobs.

Editor access is rechecked by the gateway, and queued starts recheck captured-group
authority before dispatch. Use Terraform permission assignments to grant both
DataContributor and Executor for development, plus Contributor when developers may create
their own environments. Give these grants in dev only when production editors are
not permitted. Interactive editors necessarily expose their private filesystem;
checkout/pull inside the editor cannot be isolated from arbitrary shell execution.

## Run content and controls

Run ownership is a privacy boundary, not permission to read its contents. Named grants
can target `run:<id>`, its private `workspace:<id>`, the `workspaces` collection, or
its workload parent. The server derives the run's layer and rejects conflicting grants.
Use namespace `artifacts` Reader for output names/sizes and DataReader for content and
rendered app access. Logs require `logs.read`; cancellation requires `runs.cancel_own`
or `runs.cancel_others`. The app gateway rechecks `artifacts.open` on each authorization.
Legacy explicit workload execution still supplies its previous run-content authority
while that domain migrates. Workspace owners receive no corresponding implicit grant.
Interactive editors expose files on their private volume; artifact API restrictions do
not hide a file from someone separately authorized to open that editor.


## Data, platform and privileged access

Access → Groups → Has access to lists every role a group holds across **all layers**, once, under
**Data access**, **Platform access** or **Privileged access**: object roles, roles through data
products, roles on Administration pages, named feature grants, custom roles and review duties. Each
role shows its object, origin, layer and conditions (approval expiry, a form deadline, a duty's
scope) and opens to the permissions it carries, each with its own kind. Access → Roles lists the
same for every role each kind of object offers (`GET /v1/permissions/catalog`). Viewing a grant is
not a bypass of active-group, profile, reviewed-access or execution-layer checks.

The roles that read and change data are separate from the ones that configure an object, and from
the ones that give access: a Contributor cannot read or write rows, a Data Contributor cannot alter
tables or give access, and an Access Administrator can give access without reading data. Use the
matrix for each concrete function.

Review authority is privileged access: deciding who gets access. New assignments can use namespace
`reviews`, with `reviews.forms.review`, `reviews.memberships.approve` or
`reviews.configuration.manage`, scoped to the case/object/group and layer. Matching
case/membership function permissions, self-review restrictions and review-step
checks still apply. Existing review assignments retain their classification/group
conditions and appear as privileged roles in the same inventory.
The new namespace prevents existing broad compliance function roles from silently
acquiring review-step eligibility.

Backup destinations, encryption-key references and runtime key references use the
installation's selected secret backend. New archives do not contain secret values;
see [backup plans and recovery](platform-recovery.md).
