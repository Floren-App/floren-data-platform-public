# Floren Data Platform

FDP is an open-source, governed compute layer for data teams. It brings workspaces,
compute, governed SQL, published workloads and a catalog together on Kubernetes.
Develop in a private workspace, share code through Git, and publish repeatable jobs or
dashboards with their own managed identities.

It is built on three principles:

- **Security by design.** Every actor has an identity, permissions are explicit, and
  credentials never reach user code.
- **Bring your own infrastructure.** Use your own identity provider, secret store
  (OpenBao, Vault, Kubernetes, AWS, Azure or Google), PostgreSQL and object storage, or switch
  on the optional bundled Keycloak, PostgreSQL, RustFS and OpenBao.
- **No vendor lock-in.** Only open-source components, open data formats and standard
  interfaces, on any Kubernetes in any cloud or on-premises.

Read the [design principles](architecture/index.md) to see how these shape the platform.

## Choose a starting point

| You want to… | Start here |
|---|---|
| Try the application locally | [Quickstart](getting-started.md) |
| Install it on Kubernetes | [Installation](kubernetes-install.md) |
| Understand the design and its security model | [Architecture](architecture/index.md) |
| See what you can bring and what is bundled | [Bring your own infrastructure](architecture/bring-your-own.md) |
| Understand the object and identity model | [Core concepts](concepts.md) |
| Configure everything as code | [Terraform/OpenTofu](terraform.md) |
| Write a program or dashboard | [Workspaces](workspaces.md), then [fdp.json](fdp-json.md) |
| Build a complete data product | [End-to-end example](pokemon.md) |
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
their workload compute and data-access infrastructure. Layers are defined by you, for
example Dev, Test and Prod.

## Documentation and versions

These guides describe the source revision published with this site. Generated
references come from that same revision. The public site currently tracks `main`;
it is not an archive of every installed release. For an older installation, read
the Markdown at its Git commit before applying configuration changes.

Public guides use example names and addresses. Installation-specific configuration,
secrets, Terraform state and operational logs belong in your private repositories.
See [feature boundaries](feature-status.md) for current limitations.
