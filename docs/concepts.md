# Core concepts

| Concept | Meaning |
|---|---|
| Control plane | FDP API, metadata, authorization, catalog and background coordination |
| Layer | An isolation and placement boundary for workspaces, data and workloads |
| Workspace | A person's private editor and persistent project files in one layer |
| Compute pool | A named execution size and concurrency budget |
| Workload | A Git-published script, app, dbt project or Airflow deployment |
| Workload folder | A nested organizational container with inherited IAM |
| App group | A security group owned by FDP, bound to directory identities |
| Managed identity | The built-in service principal of a registered workload |
| Storage integration | A connection to PostgreSQL, Iceberg or object storage |
| Schema | The initial boundary for SQL data permissions, including future objects |
| Data product | A governed offering of usable data/apps; its designation does not change the physical object type |
| Catalog | Search, metadata, governance, lineage, quality and service-level evidence |

## Groups are not folders

App groups hold security bindings and receive roles on resources. Workload folders
organize apps/programs and inherit permissions from their parents. A folder called
Finance and a security group called Finance analysts are independent objects.

The identity chain is: permission → resource role assignment → app group → IdP
group/role/user → person. A service identity may also receive a direct role assignment.
See the complete [role matrices](rbac.md).

## Primary and secondary groups

Your active app group authorizes creation and owns new objects. Other memberships
continue to authorize operations on existing objects. Workloads, Storage and Catalog
discovery use the active group; object visibility and authorization are distinct.
Global Admin has protected platform-wide access. See [active groups](active-groups.md).

## Execution is separate from viewing

Permission to open a dashboard does not automatically grant permission to all its
underlying data. A publication can use its fixed managed identity or caller data
permissions. In caller mode, shared dashboards must delegate each viewer's data
request rather than reuse a shared privileged connection.

## Product boundaries

A table and the dashboard built on it can belong to the same usable offering.
Keep the underlying table, schema, workload and orchestrator distinct in lineage.
Current catalog governance is attached to individual resources; a separately
managed multi-asset product package is not yet a first-class Terraform resource.
