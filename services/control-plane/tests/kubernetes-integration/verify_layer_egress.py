"""Prove data-source ipBlock boundaries on disposable k3s/kube-router.

Uses cached small images, a private kubeconfig and its own Docker network/cluster.
Never uses the current context, live Compose services, or existing data volumes.
This verifies the network rules, not a complete Helm install or Trino TLS.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[4]
SERVER_IMAGE = os.environ.get('FDP_NETWORK_TEST_SERVER_IMAGE', 'fdp/control-plane:k3d-test')
os.environ['FDP_DATABASE_URL'] = 'sqlite://'
os.environ.pop('FDP_DATABASE_HOST', None)
sys.path.insert(0, str(ROOT / 'services/control-plane'))

from app import layer_egress as egress, layers
from app.layer_manifests import ControllerConfig, WorkerPlan, manifests
from app.layer_infrastructure import InfrastructureSpec, namespace_for
from app.models import DataSource


def command(args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise RuntimeError('Isolated egress verification failed: ' + Path(args[0]).name + ': ' + result.stderr[-1500:])
    return result.stdout


def main():
    suffix = uuid.uuid4().hex[:8]
    name = 'dc-egress-' + suffix
    endpoint = name + '-data'
    network = 'k3d-' + name
    cluster_created = endpoint_created = False
    with tempfile.TemporaryDirectory(prefix='fdp-egress-') as temporary:
        kubeconfig = str(Path(temporary) / 'kubeconfig')
        env = {**os.environ, 'KUBECONFIG': kubeconfig}

        def kubectl(*args, **kwargs):
            return command(['kubectl', '--kubeconfig', kubeconfig, *args], env=env, **kwargs)

        def apply(items):
            kubectl('apply', '-f', '-', input=json.dumps({'apiVersion': 'v1', 'kind': 'List', 'items': items}))

        try:
            print('Creating isolated k3d cluster', flush=True)
            command(['k3d', 'cluster', 'create', name, '--servers', '1', '--agents', '0', '--no-lb',
                '--image', 'rancher/k3s:v1.33.6-k3s1', '--kubeconfig-update-default=false',
                '--kubeconfig-switch-context=false', '--k3s-arg', '--disable=traefik@server:0',
                '--k3s-arg', '--disable=servicelb@server:0', '--wait'], env=env, timeout=180)
            cluster_created = True
            Path(kubeconfig).write_text(command(['k3d', 'kubeconfig', 'get', name], env=env))
            print('Importing cached test images', flush=True)
            command(['k3d', 'image', 'import', '-c', name, SERVER_IMAGE, 'curlimages/curl:8.11.1'], env=env, timeout=180)
            server_script = ('from http.server import HTTPServer, SimpleHTTPRequestHandler\n'
                'from threading import Thread\n'
                'Thread(target=HTTPServer(("0.0.0.0",8081),SimpleHTTPRequestHandler).serve_forever,daemon=True).start()\n'
                'HTTPServer(("0.0.0.0",8080),SimpleHTTPRequestHandler).serve_forever()\n')
            command(['docker', 'run', '--rm', '-d', '--name', endpoint, '--network', network,
                '--entrypoint', 'python', SERVER_IMAGE, '-c', server_script])
            endpoint_created = True
            print('Applying layer boundaries and data-source grants', flush=True)
            address = json.loads(command(['docker', 'inspect', endpoint]))[0]['NetworkSettings']['Networks'][network]['IPAddress']
            nodes = json.loads(kubectl('get', 'nodes', '-o', 'json'))['items']
            pod_cidrs = tuple(cidr for node in nodes for cidr in node['spec']['podCIDRs'])
            node_cidrs = tuple(a['address'] + ('/128' if ':' in a['address'] else '/32')
                               for node in nodes for a in node['status']['addresses'] if a['type'] == 'InternalIP')
            config = ControllerConfig('platform', 'integration', 'dc-test-layer', 'dc-test-runtime',
                artifact_image=SERVER_IMAGE, pod_cidrs=pod_cidrs, service_cidrs=('10.43.0.0/16',), node_cidrs=node_cidrs)
            test_ns, prod_ns = [namespace_for(layer, config.namespace_prefix) for layer in (layers.NON_PROD, layers.PROD)]
            for layer in (layers.NON_PROD, layers.PROD):
                docs = manifests(layer, 1, InfrastructureSpec(enabled=True), config, workers=WorkerPlan('test', 0.5, 1, 1))
                apply([doc for doc in docs if doc['kind'] in ('Namespace', 'NetworkPolicy')])
            apply([{'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'platform'}}])
            source = DataSource(id='abcdef012345', name='files', kind='s3', egress=['0.0.0.0/0'],
                                config={'endpoint_url': 'http://external:8080'})
            database = DataSource(id='abcdef012346', name='database', kind='postgres', egress=['0.0.0.0/0'], config={'port': 8081})
            policies, problems = egress.plan([source, database], layers.NON_PROD, config,
                egress.NetworkBoundary(config.pod_cidrs, config.service_cidrs, config.node_cidrs))
            assert not problems
            apply(policies)

            def pod(namespace, name, labels, serve=False):
                args = ['python', '-m', 'http.server', '8080', '--directory', '/tmp'] if serve else ['sh', '-c', 'sleep 600']
                return {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'namespace': namespace, 'name': name, 'labels': labels},
                    'spec': {'automountServiceAccountToken': False, 'securityContext': {'runAsUser': 1000},
                        'containers': [{'name': 'test', 'image': SERVER_IMAGE if serve else 'curlimages/curl:8.11.1',
                            'imagePullPolicy': 'Never', 'command': args}]}}

            labels = {'fdp.floren.app/layer': layers.NON_PROD, 'fdp.floren.app/managed': 'true'}
            apply([pod(test_ns, 'granted', {**labels, egress.source_label(source.id): 'true'}),
                   pod(test_ns, 'ungranted', labels),
                   pod(test_ns, 'trino', {'fdp.floren.app/layer': layers.NON_PROD, 'app.kubernetes.io/component': 'trino'}),
                   pod(prod_ns, 'private', {'test': 'private'}, serve=True),
                   pod(prod_ns, 'forged-grant', {'fdp.floren.app/layer': layers.PROD,
                       'fdp.floren.app/managed': 'true', egress.source_label(source.id): 'true'}),
                   pod('platform', 'metadata', {'test': 'private'}, serve=True),
                   # Deliberately allow destination ingress: denials below must come
                   # from source egress, not only the other layer's ingress default.
                   {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                    'metadata': {'name': 'test-destination', 'namespace': prod_ns},
                    'spec': {'podSelector': {}, 'policyTypes': ['Ingress'], 'ingress': [{}]}},
                   {'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': 'metadata', 'namespace': 'platform'},
                    'spec': {'selector': {'test': 'private'}, 'ports': [{'port': 8080}]}}])
            for namespace, names in [(test_ns, ['granted', 'ungranted', 'trino']), (prod_ns, ['private', 'forged-grant']), ('platform', ['metadata'])]:
                for pod_name in names:
                    kubectl('wait', '-n', namespace, '--for=condition=Ready', 'pod/' + pod_name, '--timeout=90s')

            def connects(pod_name, target, port, expected, namespace=test_ns):
                for _ in range(12):
                    result = subprocess.run(['kubectl', '--kubeconfig', kubeconfig, 'exec', '-n', namespace, pod_name, '--',
                        'curl', '-fsS', '--connect-timeout', '1', '--max-time', '2', 'http://%s:%s/' % (target, port)],
                        env=env, capture_output=True)
                    if (result.returncode == 0) == expected:
                        return
                    time.sleep(.5)
                raise AssertionError('Unexpected network access: %s to %s:%s (expected %s)' % (pod_name, target, port, expected))

            connects('granted', address, 8080, True)
            connects('granted', address, 8081, False)  # S3 grant does not include the database port
            connects('ungranted', address, 8080, False)
            connects('forged-grant', address, 8080, False, namespace=prod_ns)
            connects('trino', address, 8081, True)
            connects('trino', address, 8080, False)
            prod_ip = json.loads(kubectl('get', 'pod', 'private', '-n', prod_ns, '-o', 'json'))['status']['podIP']
            metadata_ip = json.loads(kubectl('get', 'pod', 'metadata', '-n', 'platform', '-o', 'json'))['status']['podIP']
            service_ip = json.loads(kubectl('get', 'svc', 'metadata', '-n', 'platform', '-o', 'json'))['spec']['clusterIP']
            for target in (prod_ip, metadata_ip, service_ip, '169.254.169.254'):
                connects('granted', target, 8080, False)
            apply([egress.disabled(policies[0])])
            connects('granted', address, 8080, False)
            print('PASS: granted S3 and Trino access, source/port isolation, Prod/platform Pod and Service denial, metadata denial, and grant removal', flush=True)
        finally:
            if endpoint_created:
                subprocess.run(['docker', 'rm', '-f', endpoint], capture_output=True)
            # Also handles a cluster create that failed after partially creating nodes.
            if cluster_created or subprocess.run(['docker', 'inspect', 'k3d-' + name + '-server-0'], capture_output=True).returncode == 0:
                subprocess.run(['k3d', 'cluster', 'delete', name], env=env, capture_output=True)


if __name__ == '__main__':
    main()
