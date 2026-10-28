import os
from pathlib import Path
import shutil
import subprocess

import pytest

from app import layers
from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import manifests, WORKER_START, WORKER_HEALTH
from .test_layer_controller import WORKERS, CONFIG


def test_tls_nodes_use_encrypted_discovery_and_have_no_plaintext_paths():
    spec = InfrastructureSpec(enabled=True, trino={'workers': 2, 'tls': {
        'mode': 'existing', 'secret_name': 'server-tls'}})
    docs = manifests(layers.NON_PROD, 1, spec, CONFIG, workers=WORKERS)
    discovery = next(d for d in docs if d['metadata']['name'] == 'fdp-trino-discovery')
    assert discovery['spec']['clusterIP'] == 'None'
    assert discovery['spec']['publishNotReadyAddresses'] is True
    assert discovery['spec']['selector']['fdp.floren.app/trino-role'] == 'coordinator'
    deployments = [d for d in docs if d['kind'] == 'Deployment' and 'trino-' in d['metadata']['name']]
    for deployment in deployments:
        name = deployment['metadata']['name']
        container = deployment['spec']['template']['spec']['containers'][0]
        data = next(d['data'] for d in docs if d['kind'] == 'ConfigMap' and d['metadata']['name'] == name)
        assert 'internal-communication.https.required=true\n' in data['config.properties']
        assert 'http-server.http.enabled=false\n' in data['config.properties']
        assert 'discovery.uri=${ENV:TRINO_DISCOVERY_URI}' in data['config.properties']
        assert 'https.hostname-verification=false' not in str(data)
        assert container['ports'] == [{'name': 'https', 'containerPort': 8443}]
        env = {e['name']: e.get('value') for e in container['env']}
        if name.endswith('-coordinator'):
            assert env['TRINO_DISCOVERY_URI'] == 'https://127.0.0.1:8443'
        else:
            assert env['TRINO_DISCOVERY_HOST'] == 'fdp-trino-discovery.fdp-layer-%s.svc' % layers.NON_PROD
            assert container['command'] == ['bash', '/etc/trino/worker-start.sh']
            assert container['livenessProbe']['exec']['command'] == ['bash', '/etc/trino/worker-health.sh']
    internal = next(d for d in docs if d['metadata']['name'] == 'trino-internal')
    assert internal['spec']['ingress'][0]['ports'] == [{'protocol': 'TCP', 'port': 8443}]
    assert internal['spec']['egress'][0]['ports'] == [{'protocol': 'TCP', 'port': 8443}]
    service = next(d for d in docs if d['kind'] == 'Service' and d['metadata']['name'] == 'fdp-trino')
    assert service['spec']['ports'] == [{'name': 'https', 'port': 8443, 'targetPort': 'https'}]


def test_legacy_plaintext_mode_does_not_gain_unsupported_tls_requirements():
    docs = manifests(layers.NON_PROD, 1, InfrastructureSpec(enabled=True), CONFIG, workers=WORKERS)
    assert not any(d['metadata']['name'] == 'fdp-trino-discovery' for d in docs)
    config = next(d for d in docs if d['kind'] == 'ConfigMap')['data']['config.properties']
    assert 'discovery.uri=http://fdp-trino:8080' in config
    assert 'internal-communication.https.required' not in config


def shell_fixture(tmp_path):
    # Match the pinned minimal image: getent and core utilities, without awk.
    for utility in ('bash', 'cat', 'mkdir', 'sleep'):
        (tmp_path/utility).symlink_to(shutil.which(utility))
    getent = tmp_path/'getent'
    getent.write_text('#!/bin/bash\n[ "${DNS_FAIL:-0}" != 1 ] || exit 2\nprintf "%s" "$DNS_ANSWERS"\n')
    getent.chmod(0o755)
    timeout = tmp_path/'timeout'
    timeout.write_text('#!/bin/bash\n[ "${DNS_TIMEOUT:-0}" != 1 ] || exit 124\nshift 3\nexec "$@"\n')
    timeout.chmod(0o755)
    data = tmp_path/'data'
    data.mkdir()
    env = {**os.environ, 'PATH': str(tmp_path),
           'TRINO_DISCOVERY_HOST': 'coordinator.test', 'DNS_ANSWERS': ''}
    return data, env


@pytest.mark.parametrize('address,uri', [('10.42.1.5', 'https://10.42.1.5:8443'),
                                      ('fd42::123', 'https://[fd42::123]:8443')])
def test_worker_start_resolves_ip_and_passes_encrypted_discovery_to_launcher(tmp_path, address, uri):
    data, env = shell_fixture(tmp_path)
    env['DNS_ANSWERS'] = address+' STREAM coordinator.test\n'+address+' DGRAM coordinator.test\n'
    env['URI_OUTPUT'] = str(tmp_path/'uri')
    launcher = tmp_path/'launcher'
    launcher.write_text('#!/bin/bash\nprintf "%s" "$TRINO_DISCOVERY_URI" > "$URI_OUTPUT"\n')
    launcher.chmod(0o755)
    script = WORKER_START.replace('/data/trino', str(data)).replace('/usr/lib/trino/bin/launcher', str(launcher))
    subprocess.run(['bash', '-c', script], env=env, check=True, capture_output=True, timeout=5)
    assert Path(env['URI_OUTPUT']).read_text() == uri
    assert (data/'discovery-address').read_text().strip() == address


def test_worker_health_restarts_on_replaced_coordinator_but_not_dns_outage(tmp_path):
    data, env = shell_fixture(tmp_path)
    (data/'discovery-address').write_text('10.42.1.5\n')
    script = WORKER_HEALTH.replace('/data/trino', str(data))
    for answers, failed, timed_out, expected in [('10.42.1.5 STREAM\n', '0', '0', 0),
                                      ('10.42.2.6 STREAM\n10.42.1.5 STREAM\n', '0', '0', 0),
                                      ('10.42.2.6 STREAM\n', '0', '0', 1),
                                      ('', '1', '0', 0), ('', '0', '0', 0), ('', '0', '1', 0)]:
        env.update(DNS_ANSWERS=answers, DNS_FAIL=failed, DNS_TIMEOUT=timed_out)
        result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, timeout=5)
        assert result.returncode == expected
