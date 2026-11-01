# Scoped permissions and Terraform

FDP has an explicit capability registry, scoped permission assignments, custom action roles,
and Viewer gates for all desktop pages. These are separate from the existing data/workload
resource roles during migration. **The complete 506-action design is not yet enforced across
every endpoint and backend.** Do not treat a registry entry as an implemented security guarantee.

The [full matrix](permission-matrix.md) shows role columns and named-check coverage.
Download its [CSV](permission-matrix.csv) for filtering. The [design](permission-model-plan.md)
records the intended final contract; [existing resource RBAC](rbac.md) describes the domain
roles that remain in use for workloads, data, APIs, packages and products.

## What is enforced in this revision

| Area | Behavior |
|---|---|
| Named grants | Default-deny action checks on a target, globally, in one layer, or explicitly all layers. Known resource layers override caller-supplied scope. |
| Roles | Viewer, Executor, Contributor, Manager, Admin and Owner bundles from the registry; an explicit action set or custom role can replace a built-in bundle. |
| Page visibility | All 58 desktop tab kinds and aliases require their page gate. Hidden pages do not mount their content components. Navigation and open tab labels are filtered; page changes invalidate the object cache. |
| Administration | Named checks cover the administration handlers identified in the matrix, including form drafting/publication, classification changes, layer settings/infrastructure, pools, runtime registration and log configuration. |
| Privacy reads | Forms and drafts, policy inventory and review-duty inventory require explicit page/capability access. The `compliance_admin` role no longer grants access to Privacy administration pages. |
| Network allowlist | Per-layer host rules are bounded by a distinct global host ceiling. Changing the global ceiling requires its protected action. New proxy connections require signed workspace tickets and current layer policy. |
| Delegation | A scoped Owner may delegate only its capability namespace, targets, layers and held actions. Shared custom-role definitions require active Global Admin because changes affect every recipient. |
| Administrator context | Global Admin privileges apply with Global Admin active. Selecting another group also restricts backend administrator checks; this is no longer a UI-only preview. |
| Query log | Explicit global, layer, or own-principal read scope. SQL text needs separate permission; a page gate alone is insufficient. |

Existing explicit Viewer/Manager assignments on settings resources remain broad compatibility
grants. A legacy global Manager assignment must be removed when converting that recipient to
granular permissions. The compatibility grant is not implicitly converted into layer authority.
Existing workload/data/API/package/product roles continue to apply at their existing boundaries.

## Role meanings

| Role | Meaning |
|---|---|
| Viewer | Read permitted metadata; does not inherently read payloads or execute. |
| Executor | Viewer plus relevant use/run/read-content actions. |
| Contributor | Viewer plus relevant draft/content editing. |
| Manager | Viewer plus relevant operational configuration and publication. |
| Admin | Combines Executor, Contributor and Manager, plus the listed administrative/destructive actions. |
| Owner | Admin plus delegation and listed protected controls, within the target scope. |

Executor, Contributor and Manager do not automatically include one another. Use multiple
grants, Admin, or a custom action set where a person needs several powers. The matrix is the
precise bundle definition. Personal ownership, reviewer duty, mandatory data policies, and
layer isolation remain additional constraints. Stored secret values are never Viewer metadata.

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
  role         = "Viewer"
}

resource "fdp_permission_assignment" "engineer_dev_network" {
  target_id    = "set-network"
  namespace    = "network.editor_allowlist"
  layer_id     = var.dev_layer_id
  app_group_id = var.engineer_group_id
  role         = "Manager"
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

Give a group page Viewer on `set-forms`, then an assignment in `compliance.forms` on target
`set-forms`, globally. Contributor drafts forms; Manager publishes. To delegate only publication:

```hcl
resource "fdp_permission_assignment" "publish_forms" {
  target_id    = "set-forms"
  namespace    = "compliance.forms"
  app_group_id = var.privacy_publisher_group_id
  actions      = ["compliance.forms.publish"]
}
```

This does not grant Layers, Network, form deletion, or reviewer appointment. An explicit page
Viewer grant is still required. Form permissions are currently checked on the form collection;
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

The registered matrix is broader than this revision's named endpoint enforcement. Individual table,
column, file and artifact grants; complete common-role replacement for workload/data/API/package/product
roles; per-control capability wiring everywhere; workflow/draft additions; and complete non-HTTP/backend
conformance remain unfinished. Legacy resource grants continue to govern those areas. The named-check
column is generated from source to make this gap visible. Do not deploy on the assumption that all
506 actions can already be independently delegated across every backend.
