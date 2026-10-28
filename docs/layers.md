# Layers

Layers are global execution boundaries, independent of project folders, identity groups,
Git branches, and RBAC assignments. There are no built-in layers: administrators define them under
**Administration → Layers** or with OpenTofu (`fdp_layer`), named as the organisation works (for
example Dev, Test and Prod), and nothing runs until one exists.

- **Default layer:** work that names no layer goes to the default layer (`fdp_default_layer`, or
  **Make default** on the Layers page). With a single layer, that layer is the default; with several
  and none chosen, the platform asks for a layer. The default layer cannot be deleted.
- **Production:** a layer marked production (`production = true`) does not accept personal or group
  secrets; work there uses service account secrets.
- **Workspaces:** each layer allows or disallows workspaces. A workspace in a production layer remains
  restricted to that layer's objects and the user's RBAC grants; it does not grant production data
  access or bypass promotion and branch restrictions.

A person can create a private workspace in any layer that allows workspaces. Creation
does not create a security group or give access to an object. Each request requires:

`the workspace/workload layer matches the object's layer AND the actor has an RBAC grant`

For example, someone may have Reader on both Finance Prod and Finance Non-Prod. Their
Non-Prod workspace can read only Finance Non-Prod. Another team's Non-Prod objects
remain inaccessible without their own grants. Interactive desktop users can browse the
objects they are allowed to see across layers; executing from a workspace is always bound
to its layer. Platform-admin status is stripped from execution contexts.

## Object assignment

- Storage integrations select a layer when created. Schemas, tables, views, files, and
  catalog metadata inherit that layer. An existing integration cannot change layers.
- Workload folders can select a layer or remain organization folders spanning multiple
  layers. Child workloads must match a bound parent's layer. Workloads in an unbound
  folder explicitly select a layer. Otherwise the default layer applies.
- Deployed programs/apps and their registered identities keep that layer. Workspace
  programs and runs inherit the workspace's layer. Neither program configuration nor a
  client-supplied claim can override the server-derived execution boundary.
- `fdp.json` supports `layer` on each program and as a file-level default, by name
  or stable layer ID. Workspace discovery includes only matching programs. Direct runs,
  Airflow task admission, workload and GitOps validate the declaration too. It never
  changes the workspace/workload layer or grants access. Programs without a declaration
  use the workspace/workload layer.
- APIs and service accounts select a layer too. Direct service grants across layers are
  rejected. Users and app groups may have permissions in several layers; each execution
  still narrows those permissions to its own layer.
- Personal and group secrets are refused in production layers, which use their own
  service-account secrets. Workload contexts cannot borrow credentials from another layer.
- Catalog results and detail pages show Layer, with a Layer filter and `layer:"Non-Prod"`
  search syntax. Filters and facets are computed only from authorized metadata.

Promotion creates a distinct instance in another layer. Existing branch restrictions on
Test/Prod folders remain independent promotion rules; adding a layer does not authorize
deploying or merging a protected branch. Changing folder location cannot change the
layer of an existing workload. In-use layers, the default layer and layers that still hold
retained storage cannot be deleted. With OpenTofu, destroying a layer with infrastructure retires it
and keeps it with its storage.

## Managed and unmanaged layers

A **managed layer** has infrastructure configured (`fdp_layer` with `infrastructure`, or
**Administration → Layers**). The layer controller then gives it:

- its own namespace, `fdp-<installation>-layer-<id>`, derived from the stable layer ID;
- default-deny ingress and egress network policies, opened only for documented platform
  paths and the data-source egress you admit;
- its own Trino coordinator and workers, artifact volume and resource quota;
- optionally its own PostgreSQL and RustFS, with Iceberg on that storage.

Workspaces, jobs, apps and Airflow components of that layer run only in its namespace.
See the [layer infrastructure reference](layer-infrastructure.md) for every setting, and
[layer isolation](architecture/layer-isolation.md) for what each boundary enforces and a worked example.

An **unmanaged layer** is an authorization boundary only. Its workloads run in the shared
runtime namespace, so there is no network separation between unmanaged layers. Use
managed layers wherever layers must be isolated at the network level, as with production
and non-production. Authorization and network isolation are separate controls, and both
apply to managed layers.

Runtime pods carry a stable `fdp.floren.app/layer` label. Admission and authorization never
rely on that label: the server derives every execution's layer from its own records.
In local Docker development, each workspace and its runs use a private network instead.
