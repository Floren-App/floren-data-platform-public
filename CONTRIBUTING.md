# Contributing

Contributions are welcome under the project's license, the [Apache License 2.0](LICENSE).

## Sign off your commits (DCO)

Every commit must be signed off, certifying the [Developer Certificate of Origin](https://developercertificate.org/):
that you wrote the change, or otherwise have the right to submit it under the project's license.

```sh
git commit -s -m "Describe the change"
```

This adds `Signed-off-by: Your Name <you@example.com>` to the commit message. There is no separate contributor
license agreement.

## Rules for components

- Only free, OSI-approved open-source dependencies; no paid or source-available (e.g. BSL) components.
- A new third-party component in an image goes into THIRD_PARTY_LICENSES.md. Copyleft (GPL, AGPL) components
  must stay optional, as R is.
- Configuration goes in exactly one place, Helm or OpenTofu: see docs/configuration.md.

## Tests

`make test` runs the control plane's tests; `cd apps/desktop && npx playwright test` the app's;
`terraform/provider-fdp/acceptance` the OpenTofu provider's.

### Persona journeys: the release gate for workflows and permissions

A change to the permission model, the provider, the Terraform example or a workflow also runs
`make persona-acceptance`. Each persona (engineer, analyst, viewer, privacy reviewer, Global Admin and a workload
identity) gets its access as an installation's Terraform gives it, through reusable policies and never a grant on one
workspace ID. It then completes its journey on fresh IDs, with both the browser's and the editor's credentials, and is
refused what it should not do. The target writes `.local/persona-report.json` with the revisions and each journey's
outcome, and no secrets or data.

These journeys run against the in-process API with a simulated orchestrator, and the report says so. A live check of
an installation is a separate step.
