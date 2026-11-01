# Installing Floren Data Platform on Kubernetes

One Helm install for the platform, then one OpenTofu apply for its configuration. This page is
the short path; [the chart README](../deploy/helm/fdp/README.md) has every option, and
[operations](kubernetes-operations.md) covers upgrades, backups, rotation and recovery.

## 1. Prerequisites

| Need | Why |
|---|---|
| Kubernetes 1.27 or later | Pod Security labels and server-side apply |
| A network plugin that **enforces** NetworkPolicy (Cilium, Calico, k3s's built-in) | Layer isolation is enforced by policies; a plugin that ignores them silently removes it |
| A default StorageClass | Workspaces, layer artifacts and the bundled databases use dynamic volumes |
| Three DNS names and a TLS certificate covering them | `fdp.example.com`, `api.fdp.example.com`, `idp.fdp.example.com` |
| cert-manager (optional) | Only for automatic public or layer certificates |
| Images pushed to your registry | `make images`, plus the control plane; every image must name a version |

Check a cluster before installing (creates one temporary namespace, always removed):

```sh
make preflight                 # current kubectl context
make preflight CONTEXT=prod    # another context
```

It checks the version, installer permissions, the default storage class, cluster DNS, and that
a deny NetworkPolicy really blocks traffic. Any `FAIL` must be fixed first.

## 2. Install

Copy `deploy/helm/fdp/examples/minimal.yaml` to your private installation values.
It is a template: replace hostnames, network CIDRs and **all platform/runtime image
repositories/tags** with images available to your cluster. The example's
`fdp/control-plane:0.1.0` is not an assumed public registry release. Configure
`web.enabled` and the web image for browser access, or use the desktop client.
Pin reviewed image commits/digests; do not paste another installation's values.

```sh
cp deploy/helm/fdp/examples/minimal.yaml installation-values.yaml
# Edit installation-values.yaml as described above before installing.
kubectl create namespace fdp
# Every platform pod meets Pod Security "restricted": enforce it (the chart labels its other namespaces itself).
kubectl label namespace fdp pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted
kubectl -n fdp create secret tls fdp-public-tls --cert=tls.crt --key=tls.key
# Optional private CA bundle (public certificates only, including the public roots you need):
kubectl -n fdp create secret generic platform-ca --from-file=ca-bundle.pem

helm install fdp deploy/helm/fdp -n fdp \
  -f installation-values.yaml --wait --timeout 10m
```

[`examples/minimal.yaml`](../deploy/helm/fdp/examples/minimal.yaml) bundles the metadata
database, Keycloak and OpenBao (auto-unsealed by a key in a Kubernetes Secret), enables isolated layers, needs no shared NFS volume, and creates the
automation identity for OpenTofu. Replace its hostnames and `layerController.network` CIDRs.
For your own PostgreSQL, identity provider or secret store, start from
`examples/external-services.yaml`. To unseal the bundled OpenBao through your KMS instead, set
`openbao.seal.mode: kms`; see the [chart README](../deploy/helm/fdp/README.md#bundled-openbao).

### Using your own PostgreSQL, identity provider and OpenBao

Start from [`examples/external-services.yaml`](../deploy/helm/fdp/examples/external-services.yaml)
(`metadataDatabase`, `keycloak` and `bootstrap` off, `secretManagement.backend: openbao`). Nothing is
generated for you, so provide:

| What | Details |
|---|---|
| PostgreSQL | A database owned by a login role that is not a superuser (the platform creates its tables) |
| OpenBao | A KV v2 mount (default `fdp`) and a token limited to it: `path "fdp/data/*" { capabilities = ["create","read","update","delete"] }` and `path "fdp/metadata/*" { capabilities = ["read","list","delete"] }`. Renew or replace the token before it expires |
| Identity provider | An API audience (`oidc.audience`, default `fdp-api`) in access tokens; a public desktop client (`oidc.clientId`) with PKCE; a confidential client with a service account for OpenTofu; an admin role (`oidc.adminRole`) |
| Directory | SCIM provisioning from your IdP to `https://<host>/api/scim/v2` with `FDP_SCIM_TOKEN`. Provision people **and** the automation service account (its token's username), giving administrators the admin role |
| Platform Secret (`existingSecret`) | `FDP_DATABASE_URL` (`postgresql+psycopg://user:password@host:5432/db`), `FDP_SESSION_SIGNING_KEY`, `FDP_GATEWAY_CONFIG_TOKEN`, `FDP_TRINO_SIGNING_KEY` (random strings), `FDP_TRINO_JWT_PRIVATE_KEY` (an EC P-256 private key, PEM), `FDP_SCIM_TOKEN`, `FDP_OPENBAO_TOKEN`; when the egress proxy is enabled, a separate random `FDP_EGRESS_PROXY_TOKEN` |

Instead of OpenBao, `secretManagement.backend` can be `vault` (same settings, under `openbao`), `kubernetes`,
`aws`, `azure` or `gcp`. With OpenBao or Vault, `openbao.auth.method: kubernetes` or `approle` avoids a
long-lived token. Cloud secret managers use the control plane's workload identity: set
`serviceAccount.annotations` (and `podLabels` for Azure). See
[bring your own infrastructure](architecture/bring-your-own.md#secret-store).

Set `oidc.issuer` to the issuer in your tokens. If the cluster reaches the IdP by another address,
also set `oidc.jwksUrl`; HTTPS is verified against `trust.caBundleSecret`. Point the OpenTofu
provider's `token_url` at your IdP. `make test-e2e-external` installs this profile against a
PostgreSQL, a Keycloak over HTTPS and an OpenBao in server mode, all outside the platform namespace.

## 3. First sign-in

The bundled Keycloak creates the administrator `admin` with a temporary password:

```sh
kubectl -n fdp get secret fdp-keycloak -o jsonpath='{.data.initial-user-password}' | base64 -d
```

Sign in at `https://fdp.example.com`, set a new password, and add people through your
identity provider; the directory sync provisions them within a minute.

## 4. Configure with OpenTofu

First install the provider ([registry, release download or source](terraform.md#install-the-provider)). Adapt the
getting-started example's version, addresses, storage and directory group lookups
to your installation before applying it.

```sh
export FDP_CLIENT_SECRET=$(kubectl -n fdp get secret fdp-keycloak \
  -o jsonpath='{.data.automation-client-secret}' | base64 -d)
cd terraform/examples/getting-started
tofu init && tofu apply
```

[The example](../terraform/examples/getting-started/main.tf) creates Prod (a production layer) and
Non-Prod (the default layer), makes both isolated (each gets its own namespace, Trino and artifact storage), and adds a warehouse, a
storage integration with a governed schema, analysts with read access, a repository and a
deployed job. Apply waits until each layer is Ready and each workload is live. Run
`tofu plan` again: it should report no changes.

Terraform itself is under the Business Source License since 1.6; the provider is built and
tested with OpenTofu. See [the provider README](../terraform/provider-fdp/README.md).

## 5. Verify

| Check | How |
|---|---|
| Platform | `kubectl -n fdp get pods`: every pod Running |
| Layers | `GET /api/v1/admin/layers/<id>/infrastructure`: phase `Ready` |
| Isolation | Pods in `fdp-<installation>-layer-<id>` namespaces; a workload cannot reach another layer |
| Configuration | `tofu plan` shows no changes |

`make test-e2e-install` runs this whole path on a disposable k3s cluster (about 25 minutes,
needs 16 GB free Docker disk) and also exercises Iceberg on layer storage, distributed Airflow,
gateway mTLS and certificate renewal, upgrade, rollback, backup restore, key rotation and layer
retirement. `make preflight` checks a cluster before installing; `make test-preflight` proves it
refuses a cluster without NetworkPolicy enforcement.

## What is not supported yet

- Automated tests for CNIs other than k3s's kube-router (Cilium is verified manually), and IPv6.
- Iceberg on a layer's own PostgreSQL with data on external S3 (use the layer RustFS, or register an external
  Iceberg REST catalog as a storage integration).
- Highly available bundled databases or RustFS, and multi-cluster layers.
- Unmanaged layers in an install without the shared NFS volume run workspaces only.

## Private registry

If the platform's images are in a private registry (for example `ghcr.io/<org>/fdp-*`), create one
`kubernetes.io/dockerconfigjson` Secret in the release namespace and name it in the chart:

```sh
kubectl -n fdp create secret docker-registry ghcr-pull --docker-server=ghcr.io \
  --docker-username=<user> --docker-password=<token with only read:packages>
```

```yaml
registryCredentials:
  existingSecret: ghcr-pull
```

The control plane, directory sync and layer controller pull with it. With the bundled OpenBao, also create
the Secret in its namespace (default `fdp-openbao`), where the seal-key init container uses the
control-plane image. The control plane keeps a copy under
the same name in the runtime namespace and every managed layer namespace (and replaces those copies when
the Secret changes), and every workspace, job, app and layer helper pod references it. A Secret with that
name the platform did not write is never overwritten.
