"""Create the bundled OpenBao's static seal key once, then hand it to the server through memory.

Runs as an init container of every OpenBao pod. The key lives in an immutable Kubernetes Secret that is created
here, never by Helm, so a GitOps render without cluster lookups can never replace it. An existing key is reused;
it is never rewritten. The key itself is never logged.
"""

from __future__ import annotations

import base64
import os
import sys

KEY_BYTES = 32


def ensure_key(api, namespace: str, name: str) -> bytes:
    from kubernetes import client
    from kubernetes.client.rest import ApiException

    def read():
        secret = api.read_namespaced_secret(name, namespace)
        return base64.b64decode((secret.data or {}).get('key', ''))

    try:
        key = read()
    except ApiException as exc:
        if exc.status != 404:
            raise
        body = client.V1Secret(
            metadata=client.V1ObjectMeta(name=name, labels={'app.kubernetes.io/name': 'fdp-openbao'},
                                         annotations={'fdp.floren.app/purpose': 'OpenBao static seal key; '
                                                      'losing it makes the OpenBao data unreadable'}),
            type='Opaque', immutable=True,
            data={'key': base64.b64encode(os.urandom(KEY_BYTES)).decode()})
        try:
            api.create_namespaced_secret(namespace, body)
            print(f'Created the OpenBao seal key Secret {namespace}/{name}', flush=True)
        except ApiException as conflict:
            if conflict.status != 409:   # another pod created it first: use that one
                raise
        key = read()
    if len(key) != KEY_BYTES:
        raise SystemExit(f'Secret {namespace}/{name} must hold a {KEY_BYTES}-byte key under "key"; refusing to start')
    return key


def main() -> None:
    from kubernetes import client, config

    namespace = os.environ['FDP_OPENBAO_SEAL_NAMESPACE']
    name = os.environ['FDP_OPENBAO_SEAL_SECRET']
    path = os.environ['FDP_OPENBAO_SEAL_KEY_FILE']
    config.load_incluster_config()
    key = ensure_key(client.CoreV1Api(), namespace, name)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o400)
    with os.fdopen(descriptor, 'wb') as f:
        f.write(key)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:   # never print API responses that could contain the key
        print(f'Could not prepare the OpenBao seal key: {type(exc).__name__}', file=sys.stderr)
        sys.exit(1)
