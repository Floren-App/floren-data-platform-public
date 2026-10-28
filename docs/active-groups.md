# Active groups and ownership

The selector at the bottom of every sidebar chooses the user's **primary app group**.
It persists in Floren Data Platform for the stable directory identity, across pages and sign-ins.
Only app groups available through the user's live identity bindings can be selected.
Designated group ownership alone does not imply membership. IdP users, groups and roles
continue to enter through SCIM and bind into application-owned groups.

Creation uses the primary group's permissions and assigns it a direct Owner role on
new folders, workloads, storage integrations, schemas, APIs, operations and service
accounts. Other group memberships and direct grants remain active for reading, editing,
running and permission management on existing resources. They cannot supply a missing
creation privilege to the primary group. Platform configuration creation requires the
Global Admin primary group. Moving or editing an object does not change its owner.

Workloads, Storage and Catalog discovery show only the active group's direct or inherited
resource grants. Secondary groups and direct user grants do not add objects to these lists.
This includes catalog search, autocomplete, facets and lineage traversal. Ancestor names may
appear to provide navigation to a granted descendant; this does not grant access to siblings.
Selecting Global Admin shows all current and future objects. Selecting another group applies
that group's discovery scope even for a global administrator. Secondary authorization still
supports actions on existing objects; changing the browsing group does not revoke that access.
Managed identities and running jobs keep their own authorization and layer boundaries.

Switching groups refreshes object lists and clears cached metadata/previews. Workspaces remain
private to their creator (and manageable by global administrators); their listings show the
creator's name so identically named development environments can be distinguished.

This follows Snowflake's distinction between primary-role ownership/creation and
secondary-role access, adapted to Floren Data Platform's fixed roles and schema-level data model.
See [Snowflake access control](https://docs.snowflake.com/en/user-guide/security-access-control-overview#authorization-through-primary-role-and-secondary-roles).

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

The shared SQL gateway uses combined permissions for reads, writes and alteration, but
requires primary-group DDL authority at the destination for CREATE operations. A secondary
Admin grant cannot supply that authority. Job admission snapshots both ordinary data
access and primary CREATE authority, so already running jobs finish under their admitted
permissions after switches or revocation. Older admitted jobs retain their existing snapshot.

Data remains governed at schema level: current and future tables inherit the schema's IAM.
This change does not introduce individual table grants or transfer schema ownership when
someone creates a table. Catalog governance defaults to the registered resource's owner group.
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

Global administration remains secondary when another primary group is selected: it does
not supply that group's missing CREATE privileges. Interactive administration can span
layers, but execution contexts never gain an unrestricted administrator bypass. Existing
workspace/workload layer checks still reject cross-layer execution and data access.

## Migration

The migration retires generated `Personal owners <id>` groups. Administrator-owned grants
move to Global Admin; other personal grants become equivalent direct grants without sharing
them with all members of an arbitrary team. Catalog references are updated. Manually extended
groups with ongoing IdP bindings are preserved as ordinary legacy ownership groups instead
of flattening their dynamic membership into user snapshots. Existing non-personal group
grants, workspace files, workload identities and layer assignments are preserved.

New resource records store a canonical owner group alongside their normal additive role
assignments. Existing resources use an already authorized owner group where one exists;
legacy direct-user ownership remains valid without manufacturing a replacement personal group.

Existing direct-user schema grants retain access to existing data, but cannot authorize new
SQL objects. For example, a personal development schema with a direct user Admin grant also
needs an Admin/Owner grant to the chosen primary group before that user can create models
there. The migration does not automatically share a private schema with an entire team.
