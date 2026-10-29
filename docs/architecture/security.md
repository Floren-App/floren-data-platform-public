# Security by design

FDP's security model follows from one idea: **user code is untrusted, and the platform
must remain safe even when that code is wrong or hostile.** Programs, dashboards, notebooks
and DAGs run with the least authority needed. The control plane makes the decisions, and
backend credentials stay outside the code that people write.

## Principles

### Every actor has an identity

People sign in through your identity provider. Each published workload automatically gets
its own **managed identity**, so a dashboard or pipeline never borrows a person's account
or a shared service password. Separate service accounts exist for external clients and
deliberately shared identities. Every request, query and run is attributed to an effective
identity and, where different, to the identity that initiated it.

### Least privilege, explicit and additive

Permissions are fixed roles (Reader, Contributor, Admin, Owner on data; Executor through
Owner on workloads) granted on specific resources. Grants only add access. There are no
hidden deny rules or broken inheritance to reason about. Opening a dashboard and reading
its data are separate permissions. A workload's identity receives only the data grants it
needs. See [RBAC](../rbac.md).

### Credentials never reach user code

- SQL runs through the governed Trino path. Database and object-storage credentials live
  on the engine's catalogs. Workloads receive only their own short-lived execution token.
- Plain-file access uses short-lived STS credentials scoped to the granted prefixes.
- Programs reference secrets by name. Values are injected at run time from the secret
  store, are write-only through the API, and stay out of OpenTofu state.
- The bundled OpenBao keeps no root token: the one used to configure it on first start is
  revoked immediately. The control plane logs in with its Kubernetes identity and may only
  use its own KV mount.
- Workloads have no direct network route to backend databases, catalogs or the engine's
  internal endpoints.

### Context is derived on the server

Layer, identity, owning group and execution mode come from the server's records, never
from the client. A forged Trino user, group list, layer claim or proxy header grants
nothing. The gateway overwrites its own proof headers and ignores client-supplied
certificate headers. Configuration in `fdp.json` can narrow where a program runs, but it
can never grant access.

### Isolation through layers

A managed [layer](../layers.md) is a namespace with default-deny network policies, its
own SQL engine, storage and identities. Execution is always bound to one layer. Even a
global administrator's workspace cannot query another layer's data. Production layers
refuse personal secrets. The installer's preflight verifies that the cluster really
enforces network policy before you rely on it.

### Fail closed

Unknown SQL operations, native SQL pass-through, definer views and unreviewed table
properties are denied. Missing quality or freshness evidence counts as **unknown**, never
healthy. An unavailable secret store returns a clear 503 rather than a degraded fallback.
Internal endpoints without a configured token are switched off, not left open.

### Data stays on the server

Editors, compute and data run in the cluster. The SQL editor disables result export and
file transfer. Workloads install packages from the platform's package repository rather
than the internet, and workspace egress goes through an allowlist. Code moves through
Git, and data moves through governed paths.

### Auditable and recoverable

Configuration changes, permission changes, denied and outbound API calls and SQL
admissions are recorded with their identities, without storing query values or
credentials. The bundled OpenBao audits every secret-store request, with values
HMAC-hashed. Both streams can be exported to your SIEM as hash-chained, signed events (see
[audit trail and SIEM](../audit-and-siem.md)). Signing keys rotate with an overlap period. Certificates are monitored from
real TLS handshakes. Backup and restore procedures are part of the automated
installation test.

### An open supply chain

Every component is open source and pinned to an explicit version: the chart refuses
`latest` and unversioned infrastructure images. You can build every image from source
and run it from your own registry.

## Shared responsibilities

Some guarantees depend on how FDP is installed and used:

| Area | Your responsibility |
|---|---|
| Network isolation | Use a CNI that enforces NetworkPolicy, and configure the layer controller's cluster CIDRs |
| Identity | Protect the IdP, require MFA there, and deprovision leavers through SCIM |
| Secrets | Back up and protect the secret store. Protect OpenTofu state, which still contains sensitive configuration |
| Caller-mode dashboards | Pass each viewer's token on every request, and never share cached results across viewers |
| Git | Branch protection and review: FDP deploys whatever a followed branch contains |
| Bundled services | Bundled databases are single-instance and use internal connections without TLS. Use your own services where that matters |

See also [secrets and certificates](../secrets-and-certificates.md) and the
[feature boundaries](../feature-status.md).
