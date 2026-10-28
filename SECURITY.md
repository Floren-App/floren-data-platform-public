# Security

## Reporting a vulnerability

Please report security problems privately, not in public issues: use GitHub's **Report a vulnerability**
(Security → Advisories) on this repository. Include what is affected, how to reproduce it, and its impact.

You get an acknowledgement within a week. We fix confirmed problems as fast as their severity calls for, and
credit reporters in the advisory unless you prefer otherwise.

## Supported versions

Fixes go into the latest release. Until 1.0, older releases are not patched.

## Scope

Floren Data Platform's own code: the control plane, egress proxy, layer controller, desktop app, editor
extension, SDKs, Helm chart and OpenTofu provider. For problems in bundled third-party software (see
THIRD_PARTY_LICENSES.md), report them to that project too; we rebuild our images when they release a fix.
