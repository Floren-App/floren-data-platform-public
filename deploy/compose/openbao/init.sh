#!/bin/sh
# Initialise OpenBao once, unseal it on every start, and share the root token with the control
# plane through a volume. DEV ONLY: the unseal key and root token sit next to the data.
set -eu
export BAO_ADDR=http://openbao:8200
KEYS=/openbao/keys/init.json
until bao status -format=json >/dev/null 2>&1 || [ $? -eq 2 ]; do sleep 1; done   # 2 = sealed

if ! bao status -format=json | grep -q '"initialized": true'; then
  bao operator init -key-shares=1 -key-threshold=1 -format=json > "$KEYS"
  echo "OpenBao initialised"
fi
KEY=$(sed -n 's/.*"unseal_keys_b64": *\[ *"\([^"]*\)".*/\1/p' "$KEYS" | head -1)
[ -n "$KEY" ] || KEY=$(tr -d '\n ' < "$KEYS" | sed 's/.*"unseal_keys_b64":\["\([^"]*\)".*/\1/')
bao status -format=json | grep -q '"sealed": true' && bao operator unseal "$KEY" >/dev/null
export BAO_TOKEN=$(tr -d '\n ' < "$KEYS" | sed 's/.*"root_token":"\([^"]*\)".*/\1/')
printf '%s' "$BAO_TOKEN" > /openbao/keys/token
chmod 644 /openbao/keys/token
bao secrets list | grep -q '^fdp/' || bao secrets enable -path=fdp kv-v2 >/dev/null
echo "OpenBao ready (unsealed)"
