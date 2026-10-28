# Shared by seed.sh and example-storage/setup.sh: where the local stack is, and an admin login.
GATEWAY=${GATEWAY:-https://localhost:${GATEWAY_PORT:-8090}}
KEYCLOAK=${KEYCLOAK:-https://localhost:8443}
CURL=(curl -sf --cacert "$(mkcert -CAROOT)/rootCA.pem")
COMPOSE=(docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/example-storage/docker-compose.yml)
export FDP_SEED_GATEWAY="$GATEWAY"
export FDP_SEED_CA="$(mkcert -CAROOT)/rootCA.pem"
export FDP_SEED_SCIM_TOKEN="${FDP_SCIM_TOKEN:-dev-only-scim-provisioning-token}"

# Waits for the gateway, then exports a short-lived token for the example admin user.
fdp_login() {
  FDP_SEED_TOKEN='' python3 deploy/compose/seed_api.py --check-ready
  FDP_SEED_TOKEN=$("${CURL[@]}" "$KEYCLOAK/realms/fdp/protocol/openid-connect/token" \
    -d grant_type=password -d client_id=fdp-desktop -d username=admin -d password=admin \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')
  export FDP_SEED_TOKEN
}
