# RBAC

For named actions, page gates, layer-scoped grants and migration status, see
[Scoped permissions and Terraform](scoped-permissions.md) and the
[full role/action matrix](permission-matrix.md). The common-role migration is in progress;
the resource roles below still describe the workload/data/API model in use.

See [active groups and ownership](active-groups.md) for primary-group creation,
secondary-group access and the protected Global Admin group.

Floren Data Platform has one policy layer for schema access, nested workload folders, deployed
services, and service accounts:

`resource permission -> fixed app role on app group -> IdP group/role/user or service principal`

Managed service identities can also receive a fixed role directly, without an app group:
`resource permission -> fixed role -> service identity`. On an object's **Permissions** tab,
choose **Grant to → Service identity**, select a deployed program/app, API identity, or
service account, and assign a role such as Contributor. Direct service grants and group
grants add together and inherit to children; a weaker child grant never reduces parent access.
Only resource Owners can assign or revoke either kind of grant. Human and IdP membership
continues through app security groups. Disabled identities cannot receive new grants or
start new work; admitted jobs retain their permission snapshots until they finish.

Platform admins create application groups and designate owners. Group owners maintain
identity bindings. Resource Owners assign roles to groups or directly to services. Group ownership does not itself
confer permission to assign resource roles. Permissions live on the objects they govern:
open a data source, schema, deployed folder, program/app, or service account and choose
**Permissions**. Direct and inherited grants are shown together; inherited grants link to
the parent where they can be changed. **Access** shows the other side: who someone is and
what they can reach.

The **Access** area has **People**, **Groups** and **Service identities** (who), **App
profiles** (what parts of the app groups see; administrators) and **Secrets** (credentials). Its
sidebar only switches between them; each page carries its own list and detail.

- **Groups** are app-managed security groups. Their members are people, IdP groups/roles and
  service identities; they get fixed roles on objects. A group's page has **Members**, **Has
  access to** (roles on objects, directly and through data products, with end dates and deadlines
  from [access policies](access-policies.md), and its review duties) and **Settings** (name,
  owners, app profile, review duties). Changing members and changing roles on objects remain
  separate powers.
- **New group** is one page: name and owners, what its people see (app profile) and its first
  members. The group then opens on **Has access to**, where **Give access** offers the data products
  and objects you own: a product with one of its options, or an object with a role. When an access
  policy covers the data, its compliance form opens right there and the access follows the review.
- **People** shows each person's groups (and the IdP group that put them there) and everything
  those give them, for administrators and the person themselves. **Add to group** and **Remove**
  work for the groups you manage; administrators also get links to what the person changed (audit
  log) and queried (query log).
- **Service identities** lists every workload's built-in identity and the service accounts;
  administrators create, disable and delete service accounts here, and a service account's page
  shows its tokens and what it can reach.

Workload folders under Workloads are separate from groups.

### Who may use the Access and Administration pages

Every page in Access and Administration is an object you grant like any other, with two roles:
**Viewer** (see the page) and **Manager** (also change it). Sections are their parents, so a role
on a section covers all its pages, and a role on Administration covers everything. Platform
administrators (Global Admin) have all of it. Grant them at the bottom of each page, under **Who can
see or change this page** (administrators), or in Terraform:

```hcl
resource "fdp_role_assignment" "privacy_forms" {
  resource_id  = "set-forms"   # Compliance forms
  app_group_id = fdp_app_group.privacy_office.id
  role         = "Manager"
}
```

| Section (ID) | Pages (IDs) |
|---|---|
| Platform (`adm-platform`) | Layers (`set-layers`), Compute pools (`set-pools`), Runtimes (`set-runtimes`), Workspace templates (`set-template`), Alerts (`set-alerts`) |
| Security (`adm-security`) | App profiles (`set-profiles`), Network allowlist (`set-network`) |
| Privacy (`adm-privacy`) | Classifications (`set-classes`), Access policies (`set-policies`), Compliance forms (`set-forms`), Review duties (`set-duties`) |
| Access (`adm-access`) | Groups: create groups (`set-groups`), Service identities: create service accounts (`set-services`), Access register (`set-register`), Audit log (`set-auditlog`), Query log (`set-querylog`) |

The whole of Administration is `admin-root`. The app shows the pages you hold a role on; the API
checks the same roles. A platform administrator with another group than Global Admin active sees
what their groups were explicitly given; backend administrator privileges are reduced in that
context too. The earlier **compliance administrator** role no longer grants page visibility or
Privacy administration rights. Assign explicit page and capability grants. Group owners still
manage their own groups, and review duties stay scoped duties.
IdP identities and memberships carry **IdP · SCIM/OIDC** labels and are read-only here;
application groups and managed service identities carry **App-managed** labels.

The Storage sidebar and pages list only sources and schemas you hold a data role on, directly or through the parent source; a source appears when you can use it or at least one of its schemas. Global Admin has protected Owner access across resources; ordinary users still need explicit data grants. Admins add connections on **Storage** and change each one on its **Settings** tab (see [storage integrations](storage-integrations.md)). Workload folders expose **Contents**,
**Permissions**, and **Settings**; **New folder** and **Deploy here** preserve the current
location. Workload settings separate execution identity changes from normal use.
The Workloads sidebar's **+** menu offers **Folder** and **Deploy**, plus subfolder creation
and permissions shortcuts on each folder. Creating a folder as an Owner opens its
Permissions tab so app groups can be assigned immediately.

For example, create **Finance / Sales / tst**, **acp**, and **prd**. Use **Deploy here**
inside each environment folder, select the corresponding Git branch, and give each
workload a unique address (for example `finance-sales-prd`). The same repository and
program can be deployed repeatedly from different branches, with independent deployment
and execution settings. Folder contents show the branch, repository, and program.
Alternatively, put the three workloads directly in **Finance / Sales**. Assign shared
access at Finance or Sales and additional environment access below. Parent access cannot
be removed in a child, so grants intended only for test belong on tst rather than Finance.

Service accounts expose their own permissions and Owner-only tokens. Group membership
and binding management stay with the app group, rather than with resource grants.

Activity icons open the corresponding collection immediately. Their sidebars list actual
groups, services, secrets, and compute pools. The Storage tree expands from a source to its
registered schemas, tables/views, and columns; S3 namespaces expand into folders and files.
Opening a schema or table requests only that object's immediate metadata children. SQL
metadata goes through governed Trino admission and S3 listing uses read-only STS credentials
scoped to the selected namespace. Browsing never reads sample rows or file contents.

Tables and views have an explicit **Data preview** tab. Opening it executes a governed,
quoted `SELECT … LIMIT 25` using the signed-in user's data grants. Preview rows are never
prefetched. The response is not stored in HTTP caches; the UI retains it in its bounded
session cache. Large integers are represented as strings to preserve precision. Row order
is unspecified without an explicit sort.

Metadata is cached in memory for the signed-in session, shared between the tree and detail
pages, and retained for five minutes before reloading on the next open. Nothing is prefetched
or polled. Lists are paginated. **Refresh objects/columns**, or the sidebar refresh action,
requests fresh metadata. Signing out clears the cache. Ordinary users need a data-role grant;
interactive Global Admin has protected data access. Workload layer restrictions still apply.

Compute occupancy comes from the orchestrator across all users and includes persistent
apps and every Airflow web/scheduler/worker component on the pool. Jobs and service
components are shown separately; the concurrent-job limit applies to job slots. Missing
inventory is shown as unknown, not zero. Deployed scripts/notebooks are **Ready to run**:
they reserve no persistent process and remain callable through the run API and
`FDPRunOperator(workload="slug", args={...})` from Airflow. Executor permission on
the workload/folder and the configured execution identity's data grants still apply.


## Roles and inheritance

| Data role | Permissions within a schema |
|---|---|
| Reader | Read tables/views |
| Contributor | Read, insert, update, delete; no table creation/alteration/drop |
| Admin | Read/write and native DDL |
| Owner | Admin plus Floren Data Platform role assignment |

Custom stored-function execution is not granted to Reader or Contributor.

| Workload/folder role | Permissions |
|---|---|
| Executor | Discover/open/run |
| Contributor | Executor plus edit/deploy and automatic GitOps updates |
| Admin | Contributor plus create/remove programs/apps |
| Owner | Admin plus role assignment and protected identity configuration |

Parent grants always apply. Children can add permissions, never block them. There are no
custom permission bundles, deny rules, inheritance breaks, or individual-table grants. Schema grants include existing/future tables under managed creation paths.
Folder moves require source removal and destination creation rights; moves that change
inherited assignments also require Owner authority on the moved resource.

Global Admin has protected Owner access to every resource, including data. New registered
objects receive a direct Owner assignment for the active primary group, in addition to
inherited access. Only that primary group's privileges authorize creation; other groups remain
active for existing-object access. See [active groups](active-groups.md).

## Shared SQL and storage support

PostgreSQL and Iceberg use one authenticated Trino execution path.
Python/R, dbt and viewer-bound apps use the same engine and schema policy. The engine
holds backend credentials; clients receive only gateway metadata and their own API
identity. Catalog mappings must be unique and schemas must be provisioned before SQL
admission. See [Shared SQL execution](query-execution.md) for deployment and limitations.

S3 plain files retain scoped STS prefix policies, tested against RustFS 1.0.2. Reserved
Iceberg buckets cannot be accessed through file helpers. Other SQL connectors are not
available, and no native shared-secret fallback is issued.

## Set up schema access

1. Configure PostgreSQL and Iceberg catalogs on the Trino infrastructure. Register a
   Floren Data Platform source with its matching `catalog`. For PostgreSQL also set host/port/
   database and an engine-only connection secret matching the catalog, plus a separate
   write-only **Native provisioning credential** at `datasources/<source>/provisioner`.
2. Open **Storage**, open the source, and register a schema from **Schemas & usage**. Open
   the schema and choose **Set up access**. PostgreSQL
   provisioning transfers managed ownership, reconciles PUBLIC privileges and gives
   the engine login authority over that schema. Iceberg reserves the namespace in
   policy; an authorized Admin/Owner then creates it through SQL.
3. Create a group under **Access → Groups**, designate owners and add its members.
   Open the schema's **Permissions** tab and assign the group a fixed data role. Source-level
   grants apply to all its registered schemas. Global Admin retains protected Owner access.
4. Use `fdp.connect("finance-dw", schema="analytics")` or
   `fdp::connect("finance-dw", schema="analytics")`. The selected schema is the
   connection default; Trino still checks every referenced catalog/schema against the
   effective grants. dbt selects its configured target schema and uses dbt-trino.


S3 configuration includes `bucket`, `prefix`, `endpoint_url` (for compatible services),
`region`, `role_arn`, and optional `sts_endpoint_url`. Store STS signing credentials as the
source's provisioning credential (`access_key_id`, `secret_access_key`). Register logical
schema resources with a relative prefix; an empty relative prefix covers the configured
source prefix. Provision the namespace, then assign roles. Workloads receive the scoped
access key, secret key, session token, and expiry, never the signing credential. SDKs propagate
session tokens to boto3 and R SigV4. Governed Iceberg SQL receives no storage token. Recreate a
long-lived file client when its STS credentials expire.

## Deployed services and caller mode

Each workload automatically gets a stable service principal, selectable in the app-group
identity picker. Permissions **on** the workload control management/invocation. Bindings
**of** its service principal control its own data access and service-to-service calls.

The deployer selects caller mode or fixed mode. Fixed mode uses the workload's own
service principal or a service account the deployer is authorized to select. Human deployer
identities are not fixed execution identities. Contributors can deploy with the configured
identity, but cannot change that identity, environment/secret bindings, or role assignments.
Service-account Executor permits selection for execution, not token or secret management;
those operations require Owner. Git branch permissions must reflect who can modify code
that GitOps automatically deploys.

Caller-mode shared apps do not get a fixed-identity source-credential path. The gateway
provides a signed `X-FDP-Viewer` token, bound to that workload. Pass it per viewer
request to the SDK. The control plane checks current viewer permission on every call and
executes through Trino without exposing backend credentials to the app:

```python
# Streamlit: keep the token and results local to this viewer's session.
viewer = st.context.headers["X-FDP-Viewer"]
result = fdp.query("finance-dw", "select region from public.sales limit ?",
                         [10], viewer_token=viewer)
files = fdp.files("lake-files", viewer_token=viewer)
summary = files.read_json("summaries/today.json")
```

R exposes `fdp::query(..., viewer_token=...)` and
`fdp::files(..., viewer_token=...)`. Shiny obtains the signed header from its session
request (`HTTP_X_FDP_VIEWER`), never from a user-supplied name.

Delegated SQL supports PostgreSQL and Iceberg through Trino, one statement per call, up to 10,000 returned
rows and a 30-second statement timeout. Delegated file operations support S3 sources and
limit reads/writes to 5 MB and listings to 1,000 entries. Each call authorizes the viewer
again even if the signed viewer token remains valid for the browser session. Apps must keep
viewer tokens and caches separated by authorization context; platform checks cannot prevent
application code from intentionally displaying previously read data to another viewer.

## Directory provisioning

Set a strong separate `FDP_SCIM_TOKEN` to enable `/api/scim/v2` through the gateway
(`/scim/v2` directly on the control plane). Empty disables provisioning. Discovery, Users,
Groups, nested membership, equality filters, pagination, PUT, PATCH and soft deletion are
supported. Use bearer authentication with the provisioning token. Any SCIM 2.0 client can
provision the directory. Test your provider's behavior before production use.

Set `FDP_DIRECTORY_MODE=scim` for a provisioned directory (the Compose and Helm
examples use this). Unknown/unprovisioned human identities cannot sign in in this mode.
Provision the initial admin user with a SCIM role value matching `FDP_ADMIN_ROLE`.
The default `oidc` mode permits claim-backed local development identities.

OIDC signs users in. SCIM manages directory facts once a user/group has been provisioned;
stale JWT group/role claims cannot restore removed SCIM memberships. For OIDC-only local
development, verified claims populate the directory. SCIM can adopt those records without
changing their IDs. The configured issuer scopes the directory. Initial userName matching
binds the verified OIDC subject once; subsequent names cannot rebind an existing subject.
IdP role values are directory bindings, not names of Floren Data Platform permission bundles.

## Revocation

Admission and interactive requests use current policy. A job admitted before revocation
retains its schema/action snapshot and may finish. New jobs, retries, scheduled invocations,
and separately started child jobs need current permission. Only active jobs may renew
expired execution tokens; stopped runs cannot revive their context. Python/R SDKs renew job
tokens near expiry. Private editor sessions and shared-app viewers do not grandfather future
requests as jobs.

STS credentials expire at the storage service (minimum 15 minutes for AWS-style STS).
Delegated app requests are blocked as soon as policy changes are processed, but
previously issued STS credentials remain valid until they expire. SCIM delivery latency
precedes all of these timings.
