#!/bin/sh
# SQL workspace: CloudBeaver Community behind the Floren Data Platform gateway, querying through the governed
# SQL endpoint as this workspace's owner. Settings and saved scripts live on the workspace volume.
set -eu
if [ "$(id -u)" -eq 0 ]; then
    # Existing Docker volumes may predate /workspace in the image and be root-owned.
    # Fix only the mount root; never recursively change the owner's existing project files.
    chown 1000:1000 /workspace
    exec runuser -u dbeaver -m -- "$0" "$@"
fi
state=/workspace/.fdp/cloudbeaver
# The owner's CloudBeaver project is /workspace/sql: saved scripts are plain .sql files there, next to
# this workspace's files. CloudBeaver keeps its settings in .dbeaver/.
mkdir -p "$state/GlobalConfiguration/.dbeaver" "$state/user-projects" /workspace/sql/.dbeaver
ln -sfn "$state" /opt/cloudbeaver/workspace
project="$state/user-projects/${FDP_SQL_USER:-dc-${FDP_USER:?}}"
if [ -d "$project" ] && [ ! -L "$project" ]; then cp -Rn "$project/." /workspace/sql/ && rm -rf "$project"; fi
ln -sfn /workspace/sql "$project"
# The managed connection lives in that project so scripts opened on it can be saved; rewritten on every
# start, so upgrades take effect.
cp /opt/fdp/data-sources.json /workspace/sql/.dbeaver/data-sources.json
echo '{"folders": {}, "connections": {}}' > "$state/GlobalConfiguration/.dbeaver/data-sources.json"
python3 /opt/fdp/configure.py "$state"

# Served under the gateway's workspace prefix; the gateway authenticates every request.
export CLOUDBEAVER_ROOT_URI="/ws/${FDP_WORKSPACE_ID}/"
# Skip the first-run wizard. The admin account is unused: the owner is signed in from the gateway's
# user header, and data access always uses the workspace token, never a CloudBeaver credential.
export CB_SERVER_NAME="Floren Data Platform SQL"
export CB_SERVER_URL="${FDP_PUBLIC_URL:-http://localhost}/ws/${FDP_WORKSPACE_ID}/"
export CB_ADMIN_NAME="${CB_ADMIN_NAME:-workspace-admin}"
export CB_ADMIN_PASSWORD="${CB_ADMIN_PASSWORD:-$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')Aa1}"

python3 /opt/fdp/fdp-sql-proxy.py &
cd /opt/cloudbeaver
exec ./launch-product.sh
