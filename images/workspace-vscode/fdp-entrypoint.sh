#!/bin/sh
# Prepare persistent git config on the workspace volume, then start code-server as usual.
/usr/local/bin/fdp-git-setup.sh || echo "fdp: git setup skipped" >&2
exec /usr/bin/entrypoint.sh "$@"
