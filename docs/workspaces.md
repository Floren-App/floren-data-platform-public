# Workspaces

A workspace provides a private VS Code, RStudio or SQL editor in one layer.
Heavy work runs on compute pools. The owner keeps their own permissions, further
restricted by the workspace's layer. A Prod workspace is supported when the person
has the necessary access; a workspace is not inherently Non-Prod.

## Files and configuration

The volume has this layout:

```text
Private volume
├── lost+found/                 filesystem recovery; outside the editor mount
├── .fdp-home-layout-v1         initialization marker; outside the editor mount
└── workspace/                 mounted as /workspace
    ├── fdp.json
    ├── hello.py
    ├── repository/
    │   └── fdp.json
    └── .fdp/                  FDP Git/runtime state
```

Editors and workspace compute jobs mount the same `workspace/` subdirectory.
Do not add an extra `/workspace/workspace` level. Root and directly nested
repository configs are read at `/workspace/fdp.json` and
`/workspace/<repository>/fdp.json`. Repository programs are named
`<repository>/<program>` to avoid collisions. Configs deeper than one repository
level are not automatically discovered.

The VS Code runner uses FDP's config API. Its automatic file watcher currently
watches the root config; use the runner's reload action after editing a repository
config if it does not refresh automatically. [Program configuration](fdp-json.md)
describes validation and arguments.

## Lifecycle

Creating a pod is not the same as a ready editor. **Starting** includes storage
provisioning, image pulls and the editor's readiness check. Open the editor after
the workspace is Running. Startup failures appear on the workspace; see
[troubleshooting](troubleshooting.md).

Stopping a workspace retains its files. Its registration can also be deleted,
but private volumes are retained by the current lifecycle; storage cleanup is a
separate administrator action. Never delete a PVC that a job still uses.

## Existing installations and storage upgrades

New volumes initialize automatically with UID/GID 1000. Nonempty volumes using the
old root layout are deliberately refused: the platform never automatically moves
or deletes their contents. Stop the workspace and all its jobs, then either back
up and migrate the files into `workspace/` with the layout marker, or explicitly
reset its private volume. A reset deletes those files and must be intentional.

## Templates and Git

Administrators can assign workspace templates to app groups. Each member receives
their own workspace, created stopped; members do not share a volume. Template
repositories are cloned using the person's authorized Git credential. Commit work
to Git before relying on it elsewhere; a workspace volume is not a repository backup.
