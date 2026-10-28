"""Floren Data Platform API helpers for the local setup scripts (seed_rbac.py, example-storage/register.py)."""
import json
import os
import ssl
import sys
import time
import urllib.request
import urllib.error

base = os.environ['FDP_SEED_GATEWAY'].rstrip('/') + '/api'
token = os.environ['FDP_SEED_TOKEN']
scim_token = os.environ['FDP_SEED_SCIM_TOKEN']
context = ssl.create_default_context(cafile=os.environ['FDP_SEED_CA'])


def request(method, path, body=None, scim=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
        headers={'Authorization': 'Bearer ' + (scim_token if scim else token), 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, context=context, timeout=60) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def wait_ready():
    """Traefik refreshes routes asynchronously after the control plane starts."""
    deadline = time.monotonic() + 60
    while True:
        try:
            request('GET', '/scim/v2/ServiceProviderConfig', scim=True)
            return
        except urllib.error.HTTPError as error:
            if error.code not in (404, 502, 503, 504):
                raise
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                raise
        if time.monotonic() >= deadline:
            sys.exit('Seed failed: the SCIM endpoint at ' + base + '/scim/v2/ServiceProviderConfig '
                     'did not become ready within 60 seconds. Rebuild the control plane with '
                     '`docker compose -f deploy/compose/docker-compose.yml up -d --build control-plane` '
                     'and check the control-plane and gateway logs.')
        time.sleep(2)


def report_error(exc_type, error, traceback):
    if isinstance(error, urllib.error.HTTPError):
        # Do not echo request payloads or credentials from a server error page.
        print(f'Seed failed: {error.method if hasattr(error, "method") else "HTTP request"} '
              f'{error.url} returned HTTP {error.code} ({error.reason}).', file=sys.stderr)
        if error.code in (409, 422):
            try:
                detail = json.loads(error.read()).get('detail')
                print(f'  {detail}', file=sys.stderr)  # field names and messages only, never request values
            except (ValueError, AttributeError):
                pass
        if error.code == 401:
            print('Check the Keycloak login and that FDP_SCIM_TOKEN matches the running control plane.', file=sys.stderr)
        elif error.code == 404:
            print('Check that the control-plane image and gateway routes are up to date.', file=sys.stderr)
        return
    sys.__excepthook__(exc_type, error, traceback)



sys.excepthook = report_error


if __name__ == '__main__' and '--check-ready' in sys.argv:
    wait_ready()
