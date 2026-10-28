#!/usr/bin/env bash
# Bootstrap the local admin; sample data and identities are an explicit opt-in.
set -euo pipefail
case "${EXAMPLE_STORAGE:-0}" in
  0|1) ;;
  *) echo 'EXAMPLE_STORAGE must be 0 (admin only) or 1 (example storage and demo seed).' >&2; exit 2 ;;
esac
source deploy/compose/lib.sh
if [ "${EXAMPLE_STORAGE:-0}" = 0 ]; then
  FDP_BOOTSTRAP_API="$GATEWAY/api" \
    FDP_SCIM_TOKEN="$FDP_SEED_SCIM_TOKEN" \
    SSL_CERT_FILE="$FDP_SEED_CA" \
    python3 deploy/compose/bootstrap_admin.py
  echo 'Example storage and demo seed skipped (EXAMPLE_STORAGE=0). Existing objects and permissions are unchanged.'
  exit 0
fi
bash deploy/compose/example-storage/setup.sh
fdp_login
python3 deploy/compose/seed_rbac.py
