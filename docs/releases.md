# Releases and verification

Floren Data Platform is built and released by one pipeline in the public repository
(`.github/workflows/pipeline.yml`). Every pull request and push runs the full test suite; images and
releases are only produced from tested commits.

## What the pipeline checks

| Job | Checks |
|---|---|
| Control plane | The backend test suite, including the Helm chart rendering tests |
| Python SDK | The SDK test suite |
| OpenTofu provider | `go vet`, `go test`, and that the generated schema reference in the docs is current |
| Web app and editor extension | Type checks and production builds |
| Helm chart | `helm lint` and rendering of every example profile, and of a release-pinned chart |
| Documentation | A strict build with link checks, and that no installation material is published |

Each change runs only the suites it can affect: a first job compares it with the previous push (or a
pull request's base) and lists in the run summary which suites run and which were skipped. A documentation
change runs only the documentation job; the control-plane suite runs when anything its tests read changes,
including the chart, Compose files, SDK, schemas, examples and images. Release tags, the nightly run
(03:17 UTC), manual runs and changes to the pipeline itself run every suite. The control-plane tests run in
parallel (`pytest -n auto`, also in `make test`).

## What is published

| Trigger | Result |
|---|---|
| Push to `main` | Every image as `ghcr.io/floren-app/fdp-<image>:<commit sha>` and `:main` |
| Tag `v1.2.3` | The same images also as `:1.2.3` and `:1.2`, and a GitHub release with the Helm chart pinned to them |
| Tag `provider-v1.2.3` | Release `v1.2.3` of [terraform-provider-fdp](https://github.com/Floren-App/terraform-provider-fdp) |

Images are only rebuilt when the files they are built from change. Each build also gets a content tag
(`inputs-<hash>` of its input files and the ISO week); when that tag already exists, the commit's tags are
added to the existing, already signed image instead. Every image is still rebuilt at least weekly, for
base-image security updates, and a manual run with `rebuild` rebuilds all of them.

Images carry an SBOM and SLSA provenance, and are signed with Sigstore (keyless, recorded in the
public transparency log). Charts are signed the same way; provider releases with the provider's GPG key.

## Verify an image

```sh
cosign verify ghcr.io/floren-app/fdp-control-plane:1.2.3 \
  --certificate-identity-regexp '^https://github.com/Floren-App/floren-data-platform-public/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
docker buildx imagetools inspect ghcr.io/floren-app/fdp-control-plane:1.2.3 --format '{{ json .SBOM }}'
```

Pin images by digest in production and admit only signed images, for example with a Sigstore policy
controller or Kyverno.

## Verify and install a chart release

```sh
cosign verify-blob fdp-1.2.3.tgz --bundle fdp-1.2.3.tgz.sigstore.json \
  --certificate-identity-regexp '^https://github.com/Floren-App/floren-data-platform-public/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
helm install fdp fdp-1.2.3.tgz -n fdp -f installation-values.yaml
```

A released chart's defaults already point at that release's images. With GitOps, you can also keep using
the chart from the repository at a release tag.

## Make a release

Tag a tested commit on `main`: `git tag -a v1.2.3 -m "..." && git push origin v1.2.3`. For the provider,
tag `provider-v1.2.3`. Use semantic versions; database migrations are additive, so patch and minor
releases can be rolled back with the chart (see [operations](kubernetes-operations.md#upgrade-and-rollback)).
