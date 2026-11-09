# Active groups and ownership

The selector at the bottom of every sidebar chooses the user's **primary app group**.
It persists in Limoon for the stable directory identity, across pages and sign-ins.
Only app groups available through the user's live identity bindings can be selected.
Designated group ownership alone does not imply membership. IdP users, groups and roles
continue to enter through SCIM and bind into application-owned groups.

Creation uses the primary group's permissions and assigns it a direct Owner role on
new folders, workloads, storage integrations, schemas, APIs, operations and service
accounts. Interactive reading, editing, execution and delegation use only the selected
group and explicit direct principal grants. Membership in an inactive group supplies no
resource authority, compliance duty or masking exemption. Platform configuration creation requires the
Global Admin primary group. Moving or editing an object does not change its owner.

Workloads, Storage and Catalog discovery show only the active group's direct or inherited
resource grants. Secondary groups and direct user grants do not add objects to these lists.
This includes catalog search, autocomplete, facets and lineage traversal. Ancestor names may
appear to provide navigation to a granted descendant; this does not grant access to siblings.
Selecting Global Admin shows all current and future objects. Selecting another group applies
that group's authority even for a global administrator. A direct URL uses the same
selected-group resource checks as discovery.
Managed identities and running jobs keep their own authorization and layer boundaries.

Switching groups refreshes object lists and clears cached metadata/previews. Workspaces remain
private to their creator (and manageable by global administrators); their listings show the
creator's name so identically named development environments can be distinguished.

The server validates membership and permission at admission. A group switch affects
subsequent requests; an in-flight request keeps its captured group through ownership
assignment. Revoked membership cannot authorize new creation under a stale selection.
No group is created automatically for a person or workspace. A user without an eligible
app group needs an administrator to bind an existing group before creating objects.

## Workspaces, programs and SQL

Workspace ownership records the selected app group. Editor files and execution sessions
retain their creator's stable identity; membership in that group does not give another
person the creator's editor session or personal credentials. Global administrators can
inspect/manage these workspaces. Workspace volumes are unchanged.

An interactive workspace run captures the currently selected primary group. Editor
execution tokens retain their workspace group, and managed workload identities retain
the workload's owning group. Managed identities keep their explicit/direct data grants;
they do not need an intermediate group to perform authorized data operations.

Execution credentials also stay within the context they were issued for when they reach runs: their details,
logs, outputs, opening, stopping and retrying.

| Credential | Runs it can reach |
|---|---|
| Workspace editor | Runs started in that workspace, not those of the owner's other workspaces |
| Run | Its own run and the runs of the workspace or workload it belongs to |
| Workload (for example an Airflow DAG) | That workload's runs, and the deployed runs its identity started: the DAG's tasks |

The role checks still apply within that context. Other runs answer as if they do not exist. In the browser, a
person keeps access to the runs of all their own workspaces.

The shared SQL gateway uses selected-group and explicit principal permissions for reads, writes and alteration, and
requires primary-group DDL authority at the destination for CREATE operations. A secondary
Admin grant cannot supply that authority. Job admission snapshots both ordinary data
access and primary CREATE authority, so already running jobs finish under their admitted
permissions after switches or revocation.

Data supports schema-wide grants and individual table, column, file and directory targets.
Inheritance is additive: remove broad schema/source grants before granting narrower objects.
Registering or creating a table does not transfer schema ownership. Catalog governance defaults to the registered resource's owner group.
Personal credential scope remains separate from shared object ownership.

## Global Admin

**Global Admin** is a protected application-owned system group. The configured IdP
administrator role is permanently bound to it. Interactive global administrators can bind
additional users, IdP groups or IdP roles. Service identities cannot be bound directly or
receive global administrative elevation.

Its members have protected Owner access to all current and future objects, including data,
independent of object grants. IAM pages display this as **System · protected**. It cannot
be revoked or downgraded on an object; the group cannot be renamed or deleted. Group
membership changes take effect on subsequent authorization checks.

Global administration is inactive while another primary group is selected. It supplies
no resource or configuration bypass in that context. Interactive administration can span
layers, but execution contexts never gain an unrestricted administrator bypass. Existing
workspace/workload layer checks still reject cross-layer execution and data access.
