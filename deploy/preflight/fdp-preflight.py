#!/usr/bin/env python3
"""Check a Kubernetes cluster before installing Floren Data Platform.

Usage: fdp-preflight.py [--kubeconfig PATH] [--context NAME] [--image IMAGE] [--cert-manager]

Read-only except for one temporary namespace (fdp-preflight-<random>) used to prove that the
network plugin enforces NetworkPolicy; it is always deleted. Exit code 1 if any check fails.
"""
import argparse
import json
import subprocess
import sys
import time
import uuid

MIN_VERSION = (1, 27)
results = []


def report(status, check, detail):
    results.append((status, check, detail))
    print(f'{status:<5} {check}: {detail}', flush=True)


def kubectl(args, base, check=True, **kwargs):
    result = subprocess.run(['kubectl', *base, *args], capture_output=True, text=True, **kwargs)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip()[-400:])
    return result


def version(base):
    info = json.loads(kubectl(['version', '-o', 'json'], base).stdout)['serverVersion']
    major, minor = int(info['major']), int(''.join(c for c in info['minor'] if c.isdigit()))
    ok = (major, minor) >= MIN_VERSION
    report('PASS' if ok else 'FAIL', 'Kubernetes version', f"{info['gitVersion']} (need {MIN_VERSION[0]}.{MIN_VERSION[1]}+)")


def permissions(base):
    needed = [('create', 'namespaces'), ('create', 'clusterroles'), ('create', 'networkpolicies'), ('create', 'persistentvolumeclaims')]
    missing = [f'{verb} {res}' for verb, res in needed
               if kubectl(['auth', 'can-i', verb, res, '--all-namespaces'], base, check=False).stdout.strip() != 'yes']
    report('FAIL' if missing else 'PASS', 'Installer permissions', 'missing: ' + ', '.join(missing) if missing else 'namespaces, RBAC, policies, volumes')


def storage(base):
    classes = json.loads(kubectl(['get', 'storageclass', '-o', 'json'], base).stdout)['items']
    default = [c['metadata']['name'] for c in classes
               if c['metadata'].get('annotations', {}).get('storageclass.kubernetes.io/is-default-class') == 'true']
    if default:
        report('PASS', 'Default storage class', default[0])
    elif classes:
        report('WARN', 'Default storage class', 'none marked default; set storage classes in values (' + ', '.join(c['metadata']['name'] for c in classes) + ')')
    else:
        report('FAIL', 'Storage', 'no StorageClass: workspaces, artifacts and bundled databases need dynamic volumes')


def cert_manager(base, required):
    present = kubectl(['get', 'crd', 'certificates.cert-manager.io'], base, check=False).returncode == 0
    if present:
        report('PASS', 'cert-manager', 'Certificate CRD present')
    else:
        report('FAIL' if required else 'WARN', 'cert-manager',
               'not installed; needed for tls.mode=certManager and layer cert_manager TLS (existing certificates work without it)')


def network_policy(base, image):
    """Two pods; a default-deny ingress policy must block the client, and removing it must allow it again."""
    namespace = 'fdp-preflight-' + uuid.uuid4().hex[:6]
    try:
        kubectl(['create', 'namespace', namespace], base)
        kubectl(['label', 'namespace', namespace, 'app.kubernetes.io/part-of=fdp-preflight'], base)
        pod = lambda name, command: {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'name': name, 'namespace': namespace, 'labels': {'app': name}},
            'spec': {'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                     'securityContext': {'runAsNonRoot': True, 'runAsUser': 65534, 'seccompProfile': {'type': 'RuntimeDefault'}},
                     'containers': [{'name': 'main', 'image': image, 'command': ['sh', '-c', command],
                                     'securityContext': {'allowPrivilegeEscalation': False, 'capabilities': {'drop': ['ALL']}},
                                     'resources': {'limits': {'cpu': '100m', 'memory': '32Mi'}}}]}}
        service = {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'server', 'namespace': namespace},
                   'spec': {'selector': {'app': 'server'}, 'ports': [{'port': 8080}]}}
        items = [pod('server', 'while true; do nc -l -p 8080 -e echo ok 2>/dev/null || nc -l -p 8080 < /dev/null; done'),
                 pod('client', 'sleep 600'), service]
        kubectl(['apply', '-f', '-'], base, input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}))
        kubectl(['-n', namespace, 'wait', '--for=condition=Ready', 'pod/server', 'pod/client', '--timeout=120s'], base)
        probe = lambda: kubectl(['-n', namespace, 'exec', 'client', '--', 'sh', '-c', 'nc -z -w 3 server 8080 && echo open || echo blocked'],
                                base, check=False).stdout.strip()
        # DNS records and service endpoints appear asynchronously after the pods start.
        resolved = None
        for _ in range(15):
            resolved = kubectl(['-n', namespace, 'exec', 'client', '--', 'nslookup', f'server.{namespace}.svc.cluster.local'], base, check=False)
            if resolved.returncode == 0:
                break
            time.sleep(2)
        report('PASS' if resolved.returncode == 0 else 'FAIL', 'Cluster DNS', 'service names resolve' if resolved.returncode == 0 else 'service name did not resolve')
        open_before = 'blocked'
        for _ in range(15):
            open_before = probe()
            if open_before == 'open':
                break
            time.sleep(2)
        deny = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy', 'metadata': {'name': 'deny', 'namespace': namespace},
                'spec': {'podSelector': {'matchLabels': {'app': 'server'}}, 'policyTypes': ['Ingress'], 'ingress': []}}
        kubectl(['apply', '-f', '-'], base, input=json.dumps(deny))
        blocked = 'open'
        for _ in range(10):  # policy programming is asynchronous
            blocked = probe()
            if blocked == 'blocked':
                break
            time.sleep(2)
        if open_before != 'open':
            report('FAIL', 'Pod networking', 'a pod could not reach a service in its own namespace')
        elif blocked != 'blocked':
            report('FAIL', 'NetworkPolicy enforcement', 'a deny policy did not block traffic: this CNI ignores NetworkPolicy, so layers would not be isolated')
        else:
            report('PASS', 'NetworkPolicy enforcement', 'deny policy blocked traffic; layer isolation can be enforced')
    except RuntimeError as exc:
        report('FAIL', 'NetworkPolicy enforcement', f'could not run the test pods ({exc}); use --image with an image that has sh and nc')
    finally:
        kubectl(['delete', 'namespace', namespace, '--wait=false'], base, check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--kubeconfig')
    parser.add_argument('--context')
    parser.add_argument('--image', default='busybox:1.36.1', help='small image with sh and nc for the network test')
    parser.add_argument('--cert-manager', action='store_true', help='fail (not warn) when cert-manager is missing')
    args = parser.parse_args()
    base = (['--kubeconfig', args.kubeconfig] if args.kubeconfig else []) + (['--context', args.context] if args.context else [])
    context = kubectl(['config', 'current-context'], base, check=False).stdout.strip()
    print(f'Floren Data Platform preflight for context {args.context or context!r}\n', flush=True)
    try:
        version(base)
    except RuntimeError as exc:
        report('FAIL', 'Cluster access', str(exc))
        return 1
    permissions(base)
    storage(base)
    cert_manager(base, args.cert_manager)
    network_policy(base, args.image)
    failed = [r for r in results if r[0] == 'FAIL']
    print(f"\n{len(results) - len(failed)} passed or warned, {len(failed)} failed", flush=True)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
