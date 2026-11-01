# Permission model and functionality inventory

Planning draft · 1 November 2026. This describes proposed behavior, not implemented guarantees.
Based on the current desktop pages, control-plane routes, resource roles, and Terraform provider.
No live role assignments or infrastructure were changed for this plan.

Implementation has started. See [current enforcement and remaining migration work](scoped-permissions.md)
for what is available in source; the complete design below remains the target, not a release guarantee.

**Start with the [full role-to-action matrix](permission-matrix.md)** for the definitive proposed
permission rows, six explicit role columns, page gates and layer examples. A matching
[CSV matrix](permission-matrix.csv) supports sorting and filtering. The grouped tables in this
document provide design rationale; the full matrix takes precedence where it adds detail.

This proposal supersedes the older RBAC planning draft where it requires Viewer before execution,
individual object grants, explicit page gates, or independently assignable action permissions.
The existing implementation is described in [RBAC](rbac.md); gaps verified during the audit are below.

## Required authorization contract

Every functionality has a named action permission. Roles are convenient bundles of those actions;
they must not be the only way to configure access. Terraform must support assigning a built-in role,
an explicitly defined custom role, or individual action permissions to an app group or service principal.
Changing recipients, actions, or layer scopes requires configuration only. Adding a new application
functionality still requires code to register and enforce its action.

For interactive use, all of these must pass:

1. The person has `pages.<page>.view` (Viewer or a role containing that permission).
2. The person has the specific action on the target object or collection.
3. The grant matches the target layer, where applicable.
4. Contextual constraints pass: personal ownership, approved review duty, execution identity,
   protected resource, or an existing data-access policy.

Page roles control the shell, navigation, deep links, tabs, and rendering. Object roles control the
objects inside it. Viewer on Storage does not reveal every source, schema, table, or row. Owner on a
page does not make someone Owner of its contents. Terraform modules should emit the needed page
Viewer grants alongside object grants explicitly, making the effective result visible in the plan.
An executor also needs Viewer for an interactive page; execution alone never grants unrelated pages.

API clients, SDKs, SQL, Terraform, and running services require the same target action and layer checks.
They do not need a desktop page grant: hiding a UI is not a machine security boundary. A human calling
an API directly cannot bypass action checks. For reads that back a page, distinguish page inventory
from scoped runtime lookups so a runtime lookup cannot enumerate administration data.

No grant means denied. List/search/autocomplete/count/facet/lineage responses contain only authorized
objects. No object name leaks through parent breadcrumbs, errors, logs, exports, notifications, or
cached content. Remove inaccessible open tabs and clear affected caches when permissions change.
App profiles may curate the experience, but cannot add permissions and must not default to all features.
An empty profile and no grants must produce an empty authorized feature set.

## Common roles

Use these names consistently across the app. They are action bundles, not an unconditional rank ladder.
Executor, Contributor, and Manager are separate powers; a configuration editor need not be a runner,
and a runner need not edit code. Each includes Viewer on its own target, subject to the separate page gate.

| Role | Default meaning on the granted target | Excluded by default |
|---|---|---|
| Viewer (V) | Discover the object and read permitted metadata/configuration | Data payloads, execution, edits, secrets, sensitive logs and outputs |
| Executor (E) | Viewer plus use/run/call/read-content actions appropriate to this resource | Editing code/configuration, DDL, assigning access |
| Contributor (C) | Viewer plus edit content, code, draft definitions, or data | Operational/security settings, access grants; execution requires E separately |
| Manager (M) | Viewer plus configure operations, deploy/publish, lifecycle of ordinary children, manage scoped settings | Access grants, identity switching, security exemptions, administrative destruction |
| Admin (A) | V + E + C + M, plus explicitly listed resource administration and destructive actions | Assigning access, changing ownership, minting impersonation credentials, overriding privacy controls |
| Owner (O) | Admin plus assign/revoke roles, designate owners, and explicitly listed protected controls | Authority outside the target scope, implicit cross-layer access, reading stored secret values |

The matrix lists the smallest built-in bundle for each action. Admin includes E/C/M actions and Owner
includes Admin unless a contextual constraint prevents the operation. A custom role can contain just
one action, such as `layers.create`, without granting every Admin action. Every role definition and
custom action selection must be inspectable in Terraform and in the access explanation UI.

All pages support Viewer and Owner for page access administration. The six common role names are
available for resource types with the relevant actions. Do not invent a runnable action for a read-only
report or force a meaningless Executor/Contributor role onto it. For example, Audit log supports Viewer,
an export capability, retention administration, and Owner; nobody receives a role that can edit audit events.

### Keep sensitive operations separate

These must be independently assignable, even where a built-in role bundles several:

- Metadata read, content read, download/export, and query execution.
- Create, edit, deploy/publish/apply, start, stop, retry, delete, and irreversible retirement.
- Read logs, read outputs, inspect source, and read sensitive configuration.
- Edit identity bindings, select execution identity, bind a secret, use a secret, rotate it, mint a token.
- Assign roles, change owners, approve access, approve membership, and exempt a caller from data policy.
- Edit network policy, apply network policy, change global defaults, and authorize controller reconciliation.

Stored secret values remain write-only for humans. Execution receives only the needed reference or
short-lived credential. Source credentials, token plaintext, private keys, and backend connection
passwords are never Viewer metadata. Token creation returns plaintext only once to its authorized creator.

## Matrix conventions

**Scope** is the proposed security boundary, not a claim about current storage schema.
**G** = global; **L** = one layer; **O** = one object/collection; **P** = a personal object.
`L/O` means both object and layer must match. Each listed verb becomes its own stable action key.
All rows also require Viewer for their interactive page. V never authorizes the other verbs in a row.
Object ownership does not remove workflow, self-approval, production, or immutable-history constraints.

## Pages, catalog, development, and execution

| Functionality / page / object | Proposed separate actions and smallest built-in roles | Scope | Recommendation |
|---|---|---|---|
| Home | V: open dashboard; V on each referenced item: show its card/count | G page; L/O contents | Compute summaries only from visible records; no global totals without explicit permission. |
| Page/navigation registry, all pages and legacy aliases | V: open; O: assign page roles | G page; optional L restriction | One registry maps every tab, deep link and API inventory to its gate; aliases use the same gate. |
| Active group / layer selection | V: list eligible groups/layers; E: select an eligible context | Own session | Selecting a layer or group grants nothing. Make active-group behavior consistent between UI and API. |
| Catalog / all objects / search | V: search, suggest, list metadata, open detail | G page; L/O records | Separate catalog discovery from data consumption; prevent hidden counts and autocomplete leaks. |
| Catalog lineage / used-by / dependencies | V: read authorized graph; C: import dbt/Airflow lineage | L/O nodes and edges | Prune unauthorized nodes and identifying edges; ingestion is scoped to its originating workload. |
| Governance descriptions, ownership labels, domains | V: read; C: edit descriptive fields; M: set governance status; O: change authoritative owner | L/O asset | Separate an informational contact label from access ownership. |
| Asset/column classifications | V: read permitted labels; C: propose classification; M: apply classification; O: approve a downgrade/exemption | L/O asset/column | Downgrades can loosen controls; do not grant them through generic metadata editing. Proposal/approval workflow is new work. |
| Data products list and detail | V: discover/read description; E: use approved contents; C: edit draft description/membership; M: publish/update live bundle; A: unpublish/delete; O: assign roles | L/O product and members | Product Viewer grants no underlying data access. Added members require validation and new approval when policy requires it. |
| Product access options, masks and filters | V: inspect permitted options; C: edit draft; M: activate an option; O: change exemptions or widen offered privileges | L/O product and assets | Product options cannot bypass underlying data-policy and approval checks. |
| My access requests | V: view own; E: request/submit/renew; C: edit own draft; E: withdraw own | P request; L/O target | Cannot request for arbitrary groups; require authority to act for the selected group. |
| Requests to decide | V: view assigned queue; M: approve/reject; O: direct consumer grants/revocation | L/O target and approved duty | Review authority is explicit; page Manager alone cannot grant access to arbitrary products. |
| Workspaces collection | V: list own/explicitly shared metadata; M: create; A: administer workspace lifecycle | L collection; P/O workspace | No automatic workspace creation permission from signing in. Creation needs permitted layer, pool, runtime and primary group. |
| Individual workspace | V: metadata/config; E: start/stop; C + E: open interactive editor; M: change ordinary settings; A: delete | L/P by default | Editor access runs arbitrary code and exposes private files. Require an explicit personal-sharing grant or audited support delegation. |
| Workspace files and Git checkout | C: edit files/checkout/pull; E: read/download authorized content; O: share access to private workspace | L/P/O | Platform administration must not silently imply access to another person's files. Remote Git permissions still apply. |
| Git credentials / credential picker | V: own credential metadata; C: create/update; A: delete; E: bind/use for authorized repository | P credential; L/O use | Never list others' credentials or return plaintext. Shared credentials need explicit object grants. |
| Repositories / branches / manifests | V: list/get branches and approved manifest metadata; E: refresh/pull; C: edit repository definition; M: register; A: remove; O: bind credentials/change trust | G definition; L binding | Repository Viewer is not source-code read access. Branch protection and auto-deploy authority must align. |
| Workloads root and nested folders | V: list/open authorized contents; M: create children; M: rename/move ordinary content; A: remove; O: assign roles | L/O folder | Use layer-specific roots or explicit layer-filtered grants; a mixed-layer folder must never bridge layers. |
| Deploy wizard / new deployment | M: create/deploy in destination; E: use pool/runtime; O: select protected identity and secret bindings | L/O destination and dependencies | Every selected dependency is authorized; source code permission alone cannot create a privileged deployment. |
| Workload code and source / versions | V: status/version metadata; E: read source artifact; C: change code/draft definition; M: deploy/sync/enable GitOps | L/O workload | Code editing can exercise fixed-identity data authority after deployment; validate deploy authority and protected configuration. |
| Workload settings and identity | M: ordinary settings and runtime sizing; O: change execution identity, caller/fixed mode, env secret bindings and ownership | L/O workload and chosen identity | Separate each protected action; Contributor cannot promote itself by changing identity or secret references. |
| Workload execution / shared apps / dbt docs | V: discover/open detail; E: run/open app/open generated docs/call orchestrator; E: stop own; M: stop others/restart operational service | L/O workload | Viewer cannot execute by opening a URL. Caller-mode data requests reauthorize each viewer. |
| Runs / queue / orchestration history | V: execution metadata; E: start/retry/cancel own; M: cancel others/manage queue | L/O run and parent workload/workspace | Retry is a new admission under current policy; no reuse of revoked execution snapshots. |
| Logs | V: list stream metadata; E: read scoped logs; E: export; service E: append own logs | L/O workload/run | Separate content read/export from status visibility; redact secrets and restrict cross-user streams. |
| Run outputs / files / generated documentation | V: list approved artifact metadata; E: read/download/open; A: delete retained artifacts | L/O run/artifact | Viewer on a workload does not expose outputs; publishing an output never bypasses content permission. |
| Usage / compute occupancy | V: aggregate authorized usage; E: read detailed occupants | L/O pool/workload | Names, users, workload IDs and queue details need target visibility; broader totals require explicit usage grant. |

## Storage, data objects, streaming, and APIs

| Functionality / page / object | Proposed separate actions and smallest built-in roles | Scope | Recommendation |
|---|---|---|---|
| Storage page and source inventory | V: list authorized sources/metadata; M: register integration; A: deregister | L/O source collection | Registering connectivity is distinct from receiving data access. |
| Source connection/settings | V: redacted metadata; M: edit ordinary config; O: change credentials, network destinations, trust and secret bindings | L/O source | Changing an endpoint can redirect privileged traffic; require network-destination authorization too. |
| Source/schema permissions and provisioning | V: inspect permitted effective access; A: provision/reconcile managed namespace; O: assign/revoke roles | L/O source/schema | Remove legacy source group-grant shortcuts or route them through the same action checks. |
| Schema/namespace registration | V: list; M: register; A: deregister/delete logical namespace; O: change ownership | L/O source and schema | Registration, native CREATE SCHEMA, and destructive DROP SCHEMA are three different actions. |
| Tables/views metadata and columns | V: discover/describe; C: edit descriptive metadata; A: create/alter/drop native table/view | L/O table/view within schema | Add stable table/view resources and individual grants; schema grants may inherit to existing/future children explicitly. |
| SQL / data preview / query results | E: SELECT/read preview; C: INSERT/UPDATE/DELETE; A: DDL/TRUNCATE; E: export results | L/O every referenced schema/table/view | Viewer sees metadata only. SQL authorization derives actions from statements and checks all dependencies, including joins/views. |
| SQL query status and cancellation | V: permitted query metadata; E: read own results/cancel own; M: cancel others; E: export | L/O query and source | Query IDs are not bearer authorization. SQL text/results require separate content grants. |
| Functions / procedures / sequences | V: discover approved metadata; E: execute/use; A: create/alter/drop | L/O executable object | No blanket function execution through SELECT. Define supported invoker/definer behavior and reject unsupported enforcement. |
| S3 files / namespaces / prefixes | V: list approved metadata; E: read/download; C: write/overwrite/delete object; A: namespace lifecycle; O: grants | L/O source/prefix/file | Prefix grants are the scalable default; support exact-object grants where backend enforcement allows it. Bound STS credentials to the same scope. |
| Iceberg tables and snapshots | V: metadata; E: read; C: data commits; A: schema changes/drop/maintenance | L/O namespace/table | No raw backing-store credentials that bypass table controls; validate commit and maintenance operations separately. |
| Row filters and column masks | V: inspect authorized/redacted policy; C: draft; M: apply restrictive policy; O: remove/weaken policy or change exemptions | L/O table/column policy | Changing a mask is a security action. Draft/restrictive-change classification requires new workflow and validation. |
| Data policy normalization / expression validation | C: validate a draft within authorized schema | L/O policy/schema | Validation must not enumerate hidden groups, columns or schemas; no SQL execution side effects. |
| Kafka cluster inventory/configuration | V: list status; M: register/configure scoped cluster; A: unregister; O: identity/trust controls | L/O cluster | Registration delegates only that layer; no Global Admin requirement for ordinary scoped administration. |
| Kafka topics / topic deletion status | V: metadata/status; M: create/configure topic; A: delete; O: assign roles | L/O cluster/topic | Retention, partitions, deletion and permissions have separate actions. Topic names must not leak across layers. |
| Kafka sessions / data / observations | E: consume/commit own consumer offsets; C: produce; M: reset others' offsets; service E: report scoped observations | L/O topic and consumer group | Consumer-group scope is part of the grant; session refresh rechecks live policy. Broker enforcement must match control-plane grants. |
| API lists and definitions: internal and external | V: redacted definitions; C: edit draft; M: register/apply; A: delete; O: trust/credentials/execution identity | L/O API | Separate registration from permission to call the API or its underlying workload. |
| API endpoint groups and operations | V: discover; C: edit draft operation; M: add/apply/move; A: delete; O: grants | L/O API/group/operation | Authorize both old and new group on a move; REST and SOAP use the same rules. |
| API gateway / outbound call | E: call the specific operation | L/O API operation and bound execution context | Recheck declared method/path and caller policy on every call; no broad wildcard call permission by default. |
| API calls / request history | V: safe status metadata; E: inspect redacted payload/error; E: export; M: retention settings | L/O API/call | Payload visibility is separate from API Viewer and may reveal data beyond ordinary definitions. |
| Connection/credential issuance / delegated SQL and files | E/C/A: only the requested data operations; O: bind protected execution identity | L/O resource plus caller/service identity | Mint only narrowed credentials; enforce per-viewer access and prohibit cross-layer credential reuse. |

## Access, privacy, security, and reporting

| Functionality / page / object | Proposed separate actions and smallest built-in roles | Scope | Recommendation |
|---|---|---|---|
| People / directory / person detail | V: authorized directory metadata; E: inspect effective access; O on group: change app membership | G directory; O person/group | Self-service is an explicit own-person scope. SCIM facts stay read-only in this UI. |
| Groups collection and individual group | V: inspect authorized group; M: create/edit ordinary metadata; A: delete; O: owners/membership/IdP bindings | G/O group | Membership changes can confer every role held by the group; keep separate from group description edits. |
| Group has-access / person has-access | V: authorized grant summaries; E: sensitive access explanations/export; O on target: give/revoke access | O group/person and referenced resource | Page Manager alone cannot issue roles. Filter referenced objects the reader cannot inspect. |
| Resource permissions / effective access / identity pickers | V: safe effective-role summary; O: inspect sensitive bindings, assign/revoke role, choose eligible principals | L/O target | One policy evaluator for UI, Terraform, metadata SQL and APIs; no principal picker that lists the entire directory. |
| Service identities: workloads, APIs and service accounts | V: metadata; M: create/edit ordinary account config; A: disable/delete; O: identity bindings/roles | L/O identity | Permissions on an identity and permissions held by it are separate. Disable blocks new use; identity lifetime is audited. |
| Run-as selection / service impersonation | E: use specifically authorized identity; O: configure fixed execution binding | L/O identity + workload | Run-as permission does not permit token creation or reading secrets. |
| Service account tokens | V: redacted token metadata; O: mint/revoke/rotate | L/O identity/token | Named atomic token actions allow delegated rotation without general Owner. Plaintext returned once; never in list APIs. |
| Secrets / certificates | V: redacted names/expiry; C: write new value; M: rotate; A: delete; E: use through authorized runtime; O: grants/bindings | P or G/O definition; L/O binding | Sharing a group does not automatically authorize secret management. Production forbids personal/group secret use as today. |
| App profiles | V: inspect authorized profile; C: edit draft; M: create/apply/default; A: delete; O: assign profiles to groups | G/O profile | Profiles curate navigation only. A profile can never replace Viewer/action grants or grant rights through a missing default. |
| Classifications dictionary | V: read published labels; C: edit draft; M: publish; A: remove unused labels | G dictionary | Use runtime-safe published lookup separately from full admin inventory. Relabeling objects needs object permission. |
| Compliance forms / form versions | V: read authorized form; C: create/edit draft; M: publish; A: delete | G/O template; optional L use binding | Published form rendered for an authorized case is not access to all drafts or the forms administration page. |
| Access policy grid / policy | V: read scoped policy; C: edit draft; M: apply; O: weaken/remove protection | L/O layer + classification/target | Scoped Managers see only their layers; remove automatic compliance-admin bypass. |
| Review duties / assignments | V: inspect permitted duties; O: appoint/revoke reviewers or membership approvers | L/O target + classification/group | Duty grants confer decision authority and are security delegation. Scope decisions by layer, resource, classification and review step. |
| Compliance review queue / case detail | V: permitted case and attachments; C: own answers/comments/attachments; E: submit/withdraw/renew own; M: assigned review decision | L/O case + requester/review duty | Queue Viewer alone cannot read every case. No self-approval; verify current duty on each decision and file read. |
| Membership approval queue | V: assigned queue; M: approve/reject assigned group membership | O group + workflow; L if group duty is bound | Group membership may span layers; show its full impact and require authority over that group. |
| Access register | V: scoped entries; E: export | L/O grants and cases | A reviewer duty must not imply global register access. Export obeys identical filters. |
| Deadlines / renewals | V: scoped deadlines; E: submit renewal; M: decide within assigned duty | L/O access/case | A global page permission never reveals others' cases; notifications/deep links recheck authorization. |
| Audit log | V: scoped redacted events; E: export/detail; M: configure retention/export policy | G events or L/O target | Immutable events; no edit action. Separate global IAM audit visibility from layer-only audit. |
| Query log | V: scoped safe query metadata; E: SQL text/details/export; M: retention/recording settings | L/O query; G defaults | SQL text is sensitive. Query-log Viewer does not grant query results or data access. |
| SIEM integration/status | V: redacted status; M: configure export; O: endpoint credentials/trust | G default; L export filters | No export destination change through ordinary log viewing; preserve immutable history and filter payloads. |

## Platform administration, networking, and packages

| Functionality / page / object | Proposed separate actions and smallest built-in roles | Scope | Recommendation |
|---|---|---|---|
| Layers collection | V: list eligible layer metadata; M: create layer; A: delete unused layer; O: assign layer roles | G collection; O layer | Collection creation is explicitly delegable; eliminate hard-coded Global Admin-only create checks. |
| Individual layer settings | V: metadata; M: ordinary settings/quotas/workspace enablement; O: production designation/trust boundary | L/O layer | Default layer selection is global configuration; cannot be changed by a Dev-only Manager. |
| Default layer / global defaults | V: inspect; M: set/reset | G dedicated configuration | Global is a distinct target, not an omitted or wildcard layer ID. |
| Layer infrastructure and operation history | V: status/history; C: draft spec; M: apply/retry; A: retire/destroy; O: privileged trust/identity changes | L/O infrastructure | Validate the entire spec against capabilities; broad infrastructure editing must not become a network/IAM bypass. |
| Network policies / ingress / storage egress | V: redacted effective rules; C: draft/validate; M: apply/remove ordinary rules; O: alter enforced isolation baseline | L/O policy; G platform baseline | Per-layer policy objects. Dev Manager cannot target Prod selectors, namespace, endpoints or parent defaults. Baseline remains mandatory. |
| Workspace HTTPS network allowlist | V: inspect; C: draft host changes; M: apply/delete host rule | L/O allowlist; G hard ceiling | Split current global-only allowlist into layer policies. Platform maximum is a ceiling; a layer may narrow it, never exceed it. |
| External API destination/trust allowlist | V: redacted rules; C: draft; M: apply permitted destination; O: global trust ceiling | L/O API destinations; G control-plane ceiling | Separate from editor HTTPS allowlist and pod network policy; secure all three paths. |
| TLS/certificate inventory and layer trust | V: public expiry/status; E: authorized certificate use; M: rotate public chain; O: change trust anchors/private-key binding | G or L/O certificate | No private keys through inventory; layer trust cannot introduce a cross-layer identity. |
| Compute pools / warehouse sizes | V: specs; E: use; M: create/configure capacity and supported engines; A: delete; O: assign usage rights | G definition; L/O binding | Global size definitions do not authorize use in every layer; quotas and pool-use grants both apply. |
| Workspace capacity / limits / default warehouse | V: inspect; M: set/reset quota/default; E: consume permitted capacity | L/workload kind; G fallback defaults | Per-layer managers cannot change shared global limits or other layers' reservations. |
| Runtime catalog | V: published metadata; E: use approved runtime; C: draft definition; M: register/enable/default; A: remove | G image definition; L approved binding | Layer approval is separate from image registration. Installation-owned images stay deployment-managed. |
| Workspace templates | V: authorized template; C: edit draft; M: create/apply/update; A: delete; O: group/identity/secret bindings | L/O template + target group | Applying a template cannot create workspaces or bind repositories for groups outside its delegated scope. |
| Alert rules and event channels | V: permitted rules/events; C: draft rule; M: create/enable/edit/delete; E: acknowledge incident | L/O rule and observed targets | Target Owner is not automatic channel Owner; validate every target and destination. |
| Webhooks / deliveries | V: redacted metadata/status; E: test/read delivery details; M: create/edit/delete; O: endpoint credentials/trust | G channel or L/O channel binding | Channel availability must be granted explicitly; payload export must not reveal unauthorized objects. |
| Metric/KPI/quality definitions | V: authorized definitions/history; C: define/version; M: enable/schedule; E: measure/report observation | L/O metric and dependencies | Measuring SQL needs data grants for every dependency; report authority is bound to the producing service. |
| SLA definitions / history / incidents | V: status/history; C: draft target; M: activate target; E: evaluate/acknowledge | L/O asset/incident | Incident acknowledgment and data access are separate. Do not reveal hidden dependency values. |
| Packages / Python and R repository | V: metadata; E: download/install; C: upload new version; M: publish/yank/unyank; A: delete; O: grants | G/O package; L consumption binding | Replace automatic everyone-read/install with explicit grants. Separate draft publication from activation where supported. |
| Package imports / allowlist | V: scoped import jobs; E: request approved import; M: approve/import and configure layer allowlist; O: global source trust ceiling | G source trust; L/O policy/job/package | An import can publish executable code platform-wide; Dev import rights cannot approve it for Prod. |

## Service and platform-only interfaces

These are functionality too, but they have service authentication rather than desktop roles.
Their credentials must be scoped, rotated, and tested; a normal page role cannot call them.

| Interface | Permission model | Scope | Recommendation |
|---|---|---|---|
| SCIM users/groups/membership/disable | Dedicated directory-provisioning service actions; read/write/delete separated | G IdP/provider | No human page role substitutes for SCIM credentials. Protect Global Admin membership and bootstrap separately. |
| Trino authorize/batch/masks/filters/events | Trusted engine authentication plus original caller's exact SQL permissions | L engine + O target | Callback credential cannot authorize another layer. Signed execution context and live revocation checks remain required. |
| Kafka broker authorize/JWKS | Broker identity plus caller topic/consumer-group actions | L broker + O topic | Public signing keys may be public; authorization endpoint is authenticated and narrowly scoped. |
| Gateway forward-auth / dynamic routing | Gateway identity plus end-user E on requested app/editor/API | L/O target | A cookie or cached route is not a permanent grant. Recheck expiry/revocation and viewer identity. |
| Execution token renewal / connection leases | Active execution identity plus original admitted scope and renewal rules | L/O run | Stopped or disabled contexts cannot renew; interactive requests never inherit a run's grandfathered snapshot. |
| Logs/lineage/metric ingestion | Producer service may append only its own authorized observations | L/O producer and declared target | No arbitrary run IDs, foreign assets, or global log writes. |
| Layer controller/reconciler/GitOps | Narrow controller identity; validated desired state; attributable initiating grant | L/O controller-managed resources | Controllers must not turn an unauthorized manifest field into privileged network/IAM/secret changes. |
| Metadata SQL mutations | Same named actions as UI/API, checked for every affected record | G or L/O per operation | No generic SQL admin bypass for roles, groups, policy, secrets, or layer configuration. |
| Metrics, gateway egress configuration, SIEM export | Dedicated service read/export grants | G or L as deployed | Machine endpoints cannot enumerate another layer using a shared unscoped token. |
| Login discovery, public JSON schemas, liveness/readiness, signing public keys | Explicit public endpoint allowlist, no user data | G | These bootstrap/protocol endpoints need no Viewer. Never expose tenant configuration, secrets, or detailed dependency errors. |
| Helm/cluster setup, CNI baseline, bootstrap credentials | Infrastructure Terraform/Helm/IAM outside app page roles | G or L infrastructure | App RBAC cannot constrain a Kubernetes cluster-admin or database superuser. Inventory and minimize infrastructure identities too. |

## Layer scoping and inheritance

Represent a grant as subject + role/action set + target + scope, with optional contextual constraints.
The target layer is resolved by the server from the actual resource; it is never trusted from a request
header, browser selector, namespace string, caller-supplied ID, or Terraform label alone.

- A Dev grant applies only to Dev. Omitted layer is invalid for layer-bound targets; it never means all.
- A global definition, a layer binding, and an all-layers grant are three explicitly different scopes.
- Moving/rebinding a resource checks source removal, destination creation, layer authority and ownership
  implications. A move cannot convert a Dev object into a Prod object or carry Dev privileges into Prod.
- Additive inheritance applies only within the declared hierarchy and layer filter. Broad parent grants
  cannot be canceled by weaker child grants. Avoid all-layers/ancestor grants for ordinary engineers.
- Collection grants create children; object grants manage existing objects. Both are delegable.
- Cross-layer dashboards and reports require grants for every constituent layer. Responses must be
  filtered before pagination/counts/export, not filtered in the browser after retrieval.
- Page administration hierarchy covers page gates only. It does not make every page Owner an owner of
  all data, policies, workloads or IAM objects. Grants to object collections are separately explicit.
- Every mutable object has a stable resource ID. Sensitive subresources (network policies, bindings,
  secrets, columns, operations, artifacts) may have their own IDs and action namespaces.
- Mandatory platform network ceilings and data policies are constraints, not additive allow grants.
  Data-policy exemptions require explicit protected actions; Owner on an unrelated page cannot bypass them.

### Requested network delegation example

| Subject | Page grant | Target grants | Result |
|---|---|---|---|
| Your network operators group | Network policies Viewer | Manager on Prod and Test network-policy collections | Sees and manages Prod/Test policies; no Dev rights unless separately granted. |
| Engineer group | Network policies Viewer | Manager on Dev network-policy collection | Can experiment within Dev's permitted ceiling; Prod/Test policies, counts and deep links are unavailable. |
| Engineer group, optional read-only Test | Network policies Viewer | Viewer on Test network-policy collection | Sees Test policy metadata; cannot draft/apply/delete it. |
| Privacy office | Forms/Policies/Reviews Viewer as needed | Contributor on designated forms; Manager on designated layer access policies and assigned case reviews; Owner only for explicitly delegated review-duty assignment | No implicit Layers, Network, Storage or Workloads access; each grant is independently removable. |
| Emergency recovery identity | Explicit activation of recovery administration | Time-bounded, audited recovery scope | Switching the displayed group must never masquerade as permission reduction while retaining silent API bypass. |

An engineer who can edit a Dev workload must also be prevented from specifying Prod credentials,
service accounts, compute bindings, source endpoints, proxy routes or namespaces in a manifest.
Layer isolation must survive all indirect write paths, not just the Network policies page.

## Terraform configuration contract

Current resources cover role assignments, groups, profiles and many objects. They do not yet provide
the full model above. The following is a **proposed interface**, not valid current provider configuration:

```hcl
# Proposed new resource: explicit page gate.
resource "fdp_page_grant" "engineer_network" {
  page         = "network-policies"
  app_group_id = fdp_app_group.engineers.id
  role         = "Viewer"
}

# Proposed extension: layer scope and a canonical function collection target.
resource "fdp_role_assignment" "engineer_dev_network" {
  resource_id  = "network-policy-collection"
  layer_id     = fdp_layer.dev.id
  app_group_id = fdp_app_group.engineers.id
  role         = "Manager"
}

# Proposed new resource: one action without a broader built-in bundle.
resource "fdp_permission_assignment" "engineer_retry_dev" {
  resource_id  = "layer-infrastructure"
  layer_id     = fdp_layer.dev.id
  app_group_id = fdp_app_group.engineers.id
  actions      = ["layer_infrastructure.retry"]
}
```

The implemented provider already supports `fdp_role_assignment` with `resource_id`, `app_group_id`
or `principal_id`, and `role`. Existing page IDs such as `set-network` and `set-forms` work for current
Viewer/Manager grants, but are not proof that every endpoint or layer scope is enforced.

Provider work must include action/role discovery, custom roles, explicit global/layer/object targets,
page gates, grant CRUD/import, validation of invalid combinations, full readback and drift detection.
Use stable IDs rather than mutable labels. Terraform must be able to remove the last grant without
an implicit legacy fallback. Secrets should use write-only/external references where supported and
must never appear in plan output, audit payloads, or API readback; protect state for unavoidable values.

Custom-role assignment cannot exceed the assigner's delegation authority. Role-definition editing
is itself privileged: adding an action to a role already held by others can expand their access.
Require authority over the new actions and affected scopes, version the definition, and show the impact.

## Current gaps and migration decisions

| Current behavior / gap | Required change | Migration recommendation |
|---|---|---|
| Forms, form drafts, policies and duties have unguarded read APIs | Scope reads and require corresponding page/object actions | Split admin inventory from authorized case-rendering lookups first. |
| `compliance_admin` grants all Privacy management | Remove role-name bypass | Translate existing grants into explicit reviewed page/actions/layer assignments, then remove compatibility fallback. |
| Settings have only Viewer/Manager bundles | Introduce action registry, common roles and granular assignments | Preserve existing rights only through an explicit migration manifest; report excessive bundles for review. |
| Viewer UI still displays layer/network edit controls | Central capabilities drive every control and route | Hide unauthorized actions and block mutations server-side; test deep links and retained tabs. |
| Non-admin Layers Manager cannot create layers | Authorize collection creation through an explicit action | Remove Global Admin-only ownership helper usage for delegable creation. |
| Most data grants are schema-wide; Reader currently includes data reads | Add metadata Viewer and exact object grants | Keep legacy Reader as an explicit data-read compatibility role; do not silently rename it to metadata-only Viewer. |
| Workload Executor currently implies discover/open/run; Contributor includes execution/deploy | Introduce separate Viewer and explicit E/C/M bundles | Produce before/after permission diff; preserve intentional execution/deploy grants explicitly. |
| API/Product Consumer and package Publisher/Maintainer are domain-specific roles | Map to new action sets while retaining temporary aliases | Consumer maps to permitted use; product metadata discovery does not become data use automatically. |
| Global editor allowlist; layer infrastructure and source egress are other network paths | Add per-layer allowlist/policy objects and global ceilings | Migrate current global hosts into explicitly reviewed layer policies; retain isolation baseline. |
| Missing profile/default means all features | Default-deny page/action grants | Profiles become optional presentation settings; never authorize through them. |
| Global Admin bypass remains effective in API when another group is active | Make effective authorization context explicit and consistent | Separate daily identity from emergency recovery, or require explicit audited privilege activation. |
| Group ownership/membership and secret use confer broad powers | Explicit group, token, secret and identity actions | Inventory membership-derived escalation and secret bindings before reducing access. |
| Controllers, native SQL, temporary credentials and package feeds can bypass a UI-only fix | Enforce the same action scope at execution/storage/gateway boundaries | Block unsupported combinations rather than issuing broader credentials. |

Individual-table grants are new work and supersede the previous schema-only planning decision.
Native database administration can carry engine-specific grant authority; the existing PostgreSQL
DDL exception must be documented and verified during implementation. Do not promise table isolation
if raw storage access, view/function ownership, inherited schema grants or external database privileges
can bypass it. Native/backend support and conformance tests determine which scopes can be advertised.

## Delivery plan and acceptance criteria

| Phase | Work | Completion evidence |
|---|---|---|
| 1. Inventory and contract | Finalize action keys for every route, page, background task and resource; settle built-in bundles and explicit exceptions | Complete route-to-action manifest; no unclassified entry; generated role/action reference. |
| 2. Core evaluator and Terraform | Scoped grants, custom roles/actions, page gates, capability responses, ownership rules, provider CRUD/import | Terraform apply/read/revoke round trips; default-deny tests; no Global Admin helper for ordinary delegated actions. |
| 3. Layer and network enforcement | Per-layer network/allowlists, global ceilings, destination validation, controller authorization | Dev operator cannot read/change Prod via UI/API/Terraform/manifest/controller; authorized Prod/Test operator succeeds. |
| 4. Workloads, data and integrations | Object grants, SQL/storage/broker/API enforcement, credentials, protected identity bindings, personal workspaces | End-to-end role tests on real supported engines and credentials; no indirect escalation through code, data or network. |
| 5. All pages and workflows | Viewer guards, per-control capabilities, scoped queues/exports/logs, profiles, cache invalidation | All tab kinds and aliases tested; hidden actions cannot execute; cached data disappears after revocation. |
| 6. Migration and release | Generate grant diff, translate explicit legacy roles, remove fallbacks, document operational ownership | Reviewed before/after effective-access report; live least-privilege validation; recovery path tested separately. |

Acceptance tests must exercise each action with no grant, Viewer, E/C/M/A/O as applicable, an exact
single-action custom grant, revoked grant, another object, another layer, direct human API access,
and service execution. Include authorized positive controls so an always-deny implementation cannot pass.

Specifically test list/detail/search/suggest/count/lineage/export leaks; downloads and log streams;
query results and cancellation; create/update/delete and bulk/move operations; group membership and
custom-role escalation; source/network/identity rebindings; template reconciliation and GitOps; cookie,
token and STS lifetime; concurrent revocation; case ownership/duty changes; and secret/token redaction.

No automatic tests were added or application behavior changed for this planning document. The earlier
audit ran 24 existing authorization tests and 3 isolated reproductions of current gaps. Those results
are not evidence that this proposed model has been implemented.

## Source coverage

The companion [route and page inventory](permission-route-inventory.md) records every statically
declared control-plane route and every desktop tab kind found in this checkout, including hidden
gateway and internal routes. It is an inventory, not a statement that those routes are already secured.
Phase 1 must turn it into an enforced route-to-action manifest, including non-HTTP paths.

Primary implementation sources: [resource RBAC](../services/control-plane/app/rbac.py),
[settings access](../services/control-plane/app/settings_access.py),
[app profiles](../services/control-plane/app/app_profiles.py),
[ownership](../services/control-plane/app/ownership.py),
[desktop tabs](../apps/desktop/src/views/Tabs.tsx),
[Terraform assignments](../terraform/provider-fdp/internal/provider/iam.go), and
[layer isolation](architecture/layer-isolation.md).
