# Packages and runtimes

FDP hosts Python and R package versions so workspace and compute dependencies can
be installed through an authorized platform endpoint. Runtime images and project
dependencies are separate configuration.

## Runtime images

The installation operator chooses the platform runtime images in Helm. Additional
runtime catalog entries and default versions are governed configuration. Pin image
tags/digests and dependency versions; do not use a moving `latest` tag for promotion.
See [configuration ownership](configuration.md#runtimes).

## Project dependencies

For Python, commit a `requirements.txt` and reference it from the program in
`fdp.json`. For R, commit `renv.lock` and use the supported program requirements
setting. Environment caches accelerate repeat runs; they do not replace dependency
locks or test that a new version behaves correctly.

Workspaces and executions receive package-index settings and scoped access keys.
Do not overwrite them with a credential copied from another environment or put
package tokens into source files.

## Importing and publishing

The Packages screen manages package versions and controlled imports from upstream
registries. Python imports use wheels for configured runtime platforms; R imports
use the repository's supported source-package workflow. Missing upstream versions,
platform incompatibility or missing operating-system libraries can still prevent
installation. Check the package import task and the actual runtime version.

Publishing follows package repository/resource roles such as Publisher, Maintainer
and Owner. An administrator can configure the upstream package allowlist. Package
permissions do not grant permission to query data or execute a workload.

## Package access

Signing in does not grant repository contents. Assign Viewer for package metadata and
Executor for downloads/installation, on an individual package or the `packages` root.
Repository grants include all current and future packages. Native Python and R indexes
filter inaccessible packages; the file URLs recheck download authority. Imported packages
also have individual permission targets. Workload/service identities need their own
explicit grants, just as people do.

Contributor uploads new packages; Manager publishes/manages versions and requests
upstream imports; Executor installs already approved packages. Admin and Owner
combine these functions, with Owner controlling delegation. Legacy Publisher and Maintainer
assignments remain compatible publishing bundles. Human editor and job credentials use
the group captured at admission, plus explicit direct grants; assigning an inactive group
later does not expand an existing editor's permissions. Update the captured group's grants
or start a workspace under the appropriate group.
