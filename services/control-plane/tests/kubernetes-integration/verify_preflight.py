"""The installation preflight passes on a cluster that enforces NetworkPolicy and fails on one that does not.

Creates two disposable k3s clusters with private kubeconfigs (never the current context): the default
k3s profile (kube-router enforces NetworkPolicy) and one started with --disable-network-policy.

    make test-preflight
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[4]
PREFLIGHT = ROOT / 'deploy/preflight/fdp-preflight.py'
IMAGE = os.environ.get('FDP_PREFLIGHT_IMAGE', 'alpine:3.20')
K3S = 'rancher/k3s:v1.33.6-k3s1'


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=kwargs.pop('timeout', 600), **kwargs)


def preflight_on(label, extra_args):
    cluster = 'dc-pf-' + uuid.uuid4().hex[:6]
    with tempfile.TemporaryDirectory(prefix='fdp-preflight-') as temporary:
        kubeconfig = str(Path(temporary) / 'kubeconfig')
        try:
            print(f'Creating disposable cluster ({label})', flush=True)
            created = run(['k3d', 'cluster', 'create', cluster, '--image', K3S, '--agents', '0', '--no-lb',
                           '--kubeconfig-update-default=false', '--kubeconfig-switch-context=false',
                           '--k3s-arg', '--disable=traefik@server:0', *extra_args, '--wait'], timeout=300)
            if created.returncode:
                raise SystemExit('cluster creation failed: ' + created.stderr[-800:])
            Path(kubeconfig).write_text(run(['k3d', 'kubeconfig', 'get', cluster]).stdout)
            run(['k3d', 'image', 'import', '-c', cluster, IMAGE])
            result = run([sys.executable, str(PREFLIGHT), '--kubeconfig', kubeconfig, '--image', IMAGE])
            return result.returncode, result.stdout + result.stderr
        finally:
            run(['k3d', 'cluster', 'delete', cluster], timeout=180)


def main():
    code, output = preflight_on('NetworkPolicy enforced', [])
    print(output)
    if code != 0:
        raise SystemExit('preflight failed on a cluster that enforces NetworkPolicy')
    code, output = preflight_on('NetworkPolicy NOT enforced', ['--k3s-arg', '--disable-network-policy@server:0'])
    print(output)
    if code == 0:
        raise SystemExit('preflight passed on a cluster that does not enforce NetworkPolicy')
    if 'NetworkPolicy' not in output:
        raise SystemExit('preflight failed without naming NetworkPolicy enforcement')
    print('PASSED: preflight accepts an enforcing cluster and refuses one without NetworkPolicy enforcement')


if __name__ == '__main__':
    main()
