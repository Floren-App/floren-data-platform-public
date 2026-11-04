#!/usr/bin/env bash
# Setup action for the local example storage (make storage-example). Connecting any storage to
# Limoon takes the same two steps; see docs/storage-integrations.md.
#   1. Outside Limoon: prepare the storage and the engine catalogs (here: Compose services,
#      example-storage/trino-catalog/*.properties, and the schemas below).
#   2. In Limoon: register each location as a storage integration (register.py).
set -euo pipefail
source deploy/compose/lib.sh
fdp_login
# Step 1. Database owners create schemas in their own warehouse; Limoon governs them afterwards.
"${COMPOSE[@]}" exec -T warehouse psql -q -U warehouse_admin -d finance <<'SQL' >/dev/null
SET client_min_messages = warning;
CREATE SCHEMA IF NOT EXISTS analytics;
CREATE SCHEMA IF NOT EXISTS dbt_ana;
SQL
# Step 2.
python3 deploy/compose/example-storage/register.py
