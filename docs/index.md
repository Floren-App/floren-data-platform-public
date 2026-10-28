# Floren Data Platform

FDP brings workspaces, compute, governed storage, published workloads and a catalog
into one platform. Develop in a private workspace, share code through Git, and
publish repeatable jobs or dashboards with their own managed identities.

## Choose a starting point

| You want to… | Start here |
|---|---|
| Try the application locally | [Quickstart](getting-started.md) |
| Install it on Kubernetes | [Installation](kubernetes-install.md) |
| Understand the object and identity model | [Core concepts](concepts.md) |
| Configure everything as code | [Terraform/OpenTofu](terraform.md) |
| Write a program or dashboard | [Workspaces](workspaces.md), then [fdp.json](fdp-json.md) |
| Build a complete data product | [Pokemon walkthrough](pokemon.md) |
| Find data and its upstream dependencies | [Catalog and lineage](catalog.md) |
| Diagnose a problem | [Troubleshooting](troubleshooting.md) |

## How the pieces fit

```text
Identity provider ──SCIM──> FDP directory ──bindings──> App groups
                                                         │ permissions
Git ──> Workspaces ──> Publish ──> Workloads ──> Data products
                                   │              │
                             Managed identity   Catalog
                                   │              │
                              Layer compute     Lineage / quality / SLAs
```

The control plane owns metadata, policy and orchestration. Managed layers own
their workload compute and data-access infrastructure. Prod and Non-Prod are
initial examples of layers, not hard-coded environment names.

## Documentation and versions

These guides describe the source revision published with this site. Generated
references come from that same revision. The public site currently tracks `main`;
it is not an archive of every installed release. For an older installation, read
the Markdown at its Git commit before applying configuration changes.

Public guides use example names and addresses. Installation-specific configuration,
secrets, Terraform state and operational logs belong in your private repositories.
See [feature boundaries](feature-status.md) for current limitations.
