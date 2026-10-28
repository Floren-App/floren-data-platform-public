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
