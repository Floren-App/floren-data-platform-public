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

Installations that had the built-in **Prod** and **Non-Prod** keep them as ordinary layers with the same
IDs: Prod is marked production and Non-Prod is the default, as they behaved before. Import them once
to manage them with OpenTofu.

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
  folder explicitly select a layer. Unspecified legacy creations default to Non-Prod.
- Deployed programs/apps and their registered identities keep that layer. Workspace
  programs and runs inherit the workspace's layer. Neither program configuration nor a
  client-supplied claim can override the server-derived execution boundary.
- `fdp.json` supports `layer` on each program and as a file-level default, by name
  or stable layer ID. Workspace discovery includes only matching programs. Direct runs,
  Airflow task admission, workload and GitOps validate the declaration too. It never
  changes the workspace/workload layer or grants access. Legacy undeclared programs
  continue using the workspace/workload layer.
- APIs and service accounts select a layer too. Direct service grants across layers are
  rejected. Users and app groups may have permissions in several layers; each execution
  still narrows those permissions to its own layer.
- Personal/shared development secrets belong to Non-Prod. Other layers use their own
  service-account secrets. Workload contexts cannot borrow credentials from another layer.
- Catalog results and detail pages show Layer, with a Layer filter and `layer:"Non-Prod"`
  search syntax. Filters and facets are computed only from authorized metadata.

Promotion creates a distinct instance in another layer. Existing branch restrictions on
Test/Prod folders remain independent promotion rules; adding a layer does not authorize
deploying or merging a protected branch. Changing folder location cannot change the
layer of an existing workload. In-use layers, the default layer and layers that still hold
retained storage cannot be deleted. With OpenTofu, destroying a layer with infrastructure retires it
and keeps it with its storage.

## Compatibility and migration

Layer IDs use the existing resource identity format. The database/runtime field
`environment_id` remains an internal compatibility field containing the canonical layer ID;
new clients use `layer_id`. Legacy environment-folder IDs resolve to their global layer.
No data files or workspace volumes are moved. Existing running jobs keep their access
snapshots. New resource admission and all subsequent workspace requests use layer checks.

The initial migration maps legacy production folders to Prod and development folders to
Non-Prod, binds existing storage/APIs/services/workspaces, and records an audit event.
The Pokémon fixture's `Pokemon Test Workspace Ana` group is converted into equivalent
direct user grants and removed. Ana's private dbt schema is not granted to all developers.
New workspace setup creates no group. Other existing security groups retain their roles.

Runtime pods/containers carry a stable `fdp.floren.app/layer` label, alongside the legacy
environment label. Docker continues using isolated execution networks. Existing running
containers may retain their narrower, old folder network until restarted; authorization
immediately resolves their context to the canonical layer.

## Future: full Kubernetes network separation

Tracked follow-up; full separation is **not implemented by this change**:

1. Use a namespace/security boundary per layer, with layer-scoped service accounts,
   secrets, storage claims and admission controls that prevent label/namespace spoofing.
2. Deny cross-layer pod/service traffic by default in both directions. Apply a default-deny
   ingress/egress policy to every workload, including Airflow workers, brokers and jobs.
3. Allow only documented platform paths: authenticated control-plane/data/API gateways,
   DNS, approved Git/package mirrors, and explicitly approved external destinations.
   Gateway access must not become a route to a different layer's backend or credentials.
4. Layer-scope direct file-storage egress and separate the SQL/catalog/credential path
   where needed. Shared gateways must preserve the server-derived layer on every request.
5. Verify DNS, direct IP, service discovery, IPv4/IPv6, shared storage and service-account
   credentials cannot cross the boundary, including nested Airflow-launched workloads.

The current shared Kubernetes namespace and existing network policies do not yet provide
this full guarantee. RBAC/layer authorization and future network enforcement are separate
controls and both must pass before claiming complete network separation.
