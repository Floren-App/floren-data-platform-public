# Terraform and OpenTofu

The FDP provider manages application configuration through the same validated API
as the UI. Use OpenTofu 1.11+ for write-only credential inputs. The provider is
published from [Floren-App/terraform-provider-fdp](https://github.com/Floren-App/terraform-provider-fdp)
under the address `floren-app/fdp`.

## Install the provider

The provider is not listed in a public registry. Each [GitHub release](https://github.com/Floren-App/terraform-provider-fdp/releases)
contains zipped binaries for Linux, macOS, Windows and FreeBSD and a `SHA256SUMS` file signed with the provider's
GPG key (`terraform/provider-fdp/signing-key.asc`, fingerprint `AD3A 4658 4EB1 012F 07D2 B6BC 182A 3416 0F87 F08F`).
Install a release into a local filesystem mirror; the script verifies the signature and checksum first:

```sh
scripts/install-provider.sh 0.3.0 "$HOME/.fdp-providers"               # this machine
scripts/install-provider.sh 0.3.0 "$HOME/.fdp-providers" linux_amd64   # for example a CI runner
```

Point OpenTofu at the mirror with a CLI configuration outside any credentials file:

```hcl title="provider-installation.tfrc"
provider_installation {
  filesystem_mirror {
    path    = "/home/you/.fdp-providers"
    include = ["registry.opentofu.org/floren-app/fdp"]
  }
  direct {
    exclude = ["registry.opentofu.org/floren-app/fdp"]
  }
}
```

Set `TF_CLI_CONFIG_FILE` to its absolute filename (the `path` may also be relative to the directory you run
`tofu` in). In each configuration root:

```hcl
terraform {
  required_providers {
    fdp = {
      source  = "registry.opentofu.org/floren-app/fdp"
      version = "0.3.0"
    }
  }
}
```

### Build from source

For an unreleased change, build into the same mirror layout from a checkout, with the Go version in the
provider's `go.mod`:

```sh
target="$HOME/.fdp-providers/registry.opentofu.org/floren-app/fdp/0.3.1-dev/$(go env GOOS)_$(go env GOARCH)"
mkdir -p "$target"
(cd terraform/provider-fdp && go build -ldflags '-X main.version=0.3.1-dev' -o "$target/terraform-provider-fdp_v0.3.1-dev" .)
```

Commit each root's dependency lock file. Do not commit the mirror binary, state,
saved plan or credentials.

## Authenticate

Use an automation identity, with the required FDP groups and permissions, and a
trusted HTTPS connection. Inject its client secret through `FDP_CLIENT_SECRET`.

```hcl
provider "fdp" {
  endpoint  = "https://fdp.example.com/api"
  token_url = "https://idp.example.com/realms/fdp/protocol/openid-connect/token"
  client_id = "fdp-automation"
  # Optional for a private CA:
  # ca_file = "certificates/ca.pem"
}
```

The shown token URL is a Keycloak example; other IdPs use their own OAuth endpoint.
The directory must recognize the automation identity. Select the owner group with
`owner_group_id` or `FDP_OWNER_GROUP_ID` when appropriate. See the
[generated provider schema](reference/terraform/index.md).

For scoped page gates, named actions and custom roles, use
[`fdp_permission_assignment`](reference/terraform/fdp_permission_assignment.md) and
[`fdp_custom_role`](reference/terraform/fdp_custom_role.md). See the
[Dev network delegation example and migration limits](scoped-permissions.md).
The [full permission matrix](permission-matrix.md) and [CSV](permission-matrix.csv)
show the six common role bundles and which actions already have named checks.

## Separate configuration roots

```text
cluster/                     ArgoCD / Helm installation configuration
platform/                    layers, groups, pools, storage, templates
workloads/
└── sales-reporting/         one root per product: publications, grants, governance
```

Use separate backend state keys for platform and workload roots. Export generic
IDs and names from the platform root. Each workload root consumes those outputs and
owns its own registrations, identities' grants and publications. Keep product-specific
resources out of the shared platform root.

## Review and apply

```sh
tofu init
tofu plan -out=reviewed.tfplan
tofu show reviewed.tfplan
tofu apply reviewed.tfplan
tofu plan
```

Saved plans and state need private storage. The final plan should show no changes.
The [getting-started source example](../terraform/examples/getting-started/main.tf)
shows the resource relationships; adapt its addresses, storage, groups and provider
version to your installation. Its placeholder IdP group must exist before lookup.

## Workload identities and import

Prefer each workload's built-in managed identity: `execution_mode = "fixed"`, no
`run_as`. `managed_identity_grants` installs the required direct grants before the
provider waits for an app to start, avoiding a publication/grant dependency cycle.
Do not manage those same assignments through separate `fdp_role_assignment` resources.

Separate service accounts are for external clients or deliberately shared identities.
Import IDs and lifecycle notes are in the [provider README](../terraform/provider-fdp/README.md).
UI edits to managed configuration may appear as drift; the next reviewed apply
restores declared values. Runtime runs, logs, sessions and credentials are not
ordinary desired-state resources.
