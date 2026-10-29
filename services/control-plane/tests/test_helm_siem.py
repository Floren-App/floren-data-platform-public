"""SIEM settings in the chart: destinations, secrets from the platform Secret, mutual TLS material."""
import shutil
import subprocess

import pytest

from .test_helm import render, resource

pytestmark = pytest.mark.skipif(not shutil.which('helm'), reason='Helm is required for chart rendering checks')


def control_plane(docs):
    return resource(docs, 'Deployment', 'fdp-control-plane')['spec']['template']['spec']


def test_siem_is_off_by_default_and_needs_no_settings():
    env = {e['name'] for e in control_plane(render())['containers'][0]['env']}
    assert not {'FDP_SIEM_STDOUT', 'FDP_SIEM_SYSLOG_ADDRESS', 'FDP_SIEM_HTTP_URL', 'FDP_SIEM_OTLP_ENDPOINT'} & env


def test_every_destination_is_configured_without_inline_secrets():
    docs = render('--set', 'siem.stdout=true', '--set', 'siem.syslog.address=siem.example.com:6514',
                  '--set', 'siem.syslog.clientCertSecret=siem-client', '--set', 'siem.http.url=https://es.example.com',
                  '--set', 'siem.http.format=elastic', '--set', 'siem.otlp.endpoint=http://otel:4318')
    pod = control_plane(docs)
    env = {e['name']: e.get('value') for e in pod['containers'][0]['env']}
    assert env['FDP_SIEM_STDOUT'] == 'true' and env['FDP_SIEM_SYSLOG_TRANSPORT'] == 'tls'
    assert env['FDP_SIEM_HTTP_FORMAT'] == 'elastic' and env['FDP_SIEM_OTLP_ENDPOINT'] == 'http://otel:4318'
    assert env['FDP_SIEM_SYSLOG_CLIENT_KEY_FILE'] == '/etc/fdp/siem/tls.key'
    assert not any(k in env for k in ('FDP_SIEM_HTTP_TOKEN', 'FDP_SIEM_SIGNING_KEY', 'FDP_SIEM_OTLP_HEADERS'))
    assert {'secretRef': {'name': 'fdp-control-plane'}} in pod['containers'][0]['envFrom']
    volume = next(v for v in pod['volumes'] if v['name'] == 'siem-client')
    assert volume['secret']['secretName'] == 'siem-client'


@pytest.mark.parametrize('setting', ['siem.http.format=syslog', 'siem.syslog.transport=http',
                                     'siem.syslog.address=no-port', 'siem.http.url=ftp://x', 'siem.start=yesterday'])
def test_invalid_siem_settings_fail_before_deployment(setting):
    with pytest.raises(subprocess.CalledProcessError):
        render('--set', setting)
