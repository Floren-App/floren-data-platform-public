# Terraform and OpenTofu

The FDP provider manages application configuration through the same validated API
as the UI. Use OpenTofu 1.11+ for write-only credential inputs. The provider source
address is `registry.opentofu.org/floren/fdp`, but a public registry release is not
assumed: install a local filesystem mirror until distribution is configured.

## Install the provider from source

From the checked-out FDP source, with the Go version in the provider's `go.mod`:

```sh
mkdir -p "$PWD/.providers/registry.opentofu.org/floren/fdp/0.2.0/$(go env GOOS)_$(go env GOARCH)"
cd terraform/provider-fdp
go build -ldflags '-X main.version=0.2.0' \
  -o "../../.providers/registry.opentofu.org/floren/fdp/0.2.0/$(go env GOOS)_$(go env GOARCH)/terraform-provider-fdp_v0.2.0" .
cd ../..
```

Create a CLI configuration outside any credentials file. Replace the mirror path
with the absolute `.providers` directory created above:

```hcl title="provider-installation.tfrc"
provider_installation {
  filesystem_mirror {
    path    = "/absolute/path/to/fdp/.providers"
    include = ["registry.opentofu.org/floren/fdp"]
  }
  direct {
    exclude = ["registry.opentofu.org/floren/fdp"]
  }
}
```

Set `TF_CLI_CONFIG_FILE` to its absolute filename. In each configuration root:

```hcl
terraform {
  required_providers {
    fdp = {
      source  = "registry.opentofu.org/floren/fdp"
      version = "0.2.0"
    }
  }
}
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
