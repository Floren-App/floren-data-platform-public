"""Exercise seed entrypoint selection without contacting Docker, SCIM or an IdP."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]


def run_seed(tmp_path, setting):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'calls.jsonl'
    stub = f'#!{sys.executable}\n' + '''import json, os, pathlib, sys
tool = pathlib.Path(sys.argv[0]).name
with open(os.environ['SEED_TEST_LOG'], 'a') as out:
    out.write(json.dumps({'tool': tool, 'args': sys.argv[1:],
        'bootstrap_api': os.environ.get('FDP_BOOTSTRAP_API'),
        'ca': os.environ.get('SSL_CERT_FILE')}) + '\\n')
if tool == 'mkcert': print('/test-ca')
elif tool == 'curl': print('{"access_token":"test-token"}')
elif tool == 'python3' and sys.argv[1] == '-c':
    sys.stdin.read()
    print('test-token')
elif tool == 'docker': sys.exit('Docker must not be called by this test')
'''
    for name in ('mkcert', 'python3', 'bash', 'curl', 'docker'):
        path = bin_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    env = {**os.environ, 'PATH': str(bin_dir)+os.pathsep+os.environ['PATH'],
           'SEED_TEST_LOG':str(log), 'GATEWAY':'https://seed.test:9090'}
    env.pop('EXAMPLE_STORAGE', None)
    if setting is not None:
        env['EXAMPLE_STORAGE'] = setting
    result = subprocess.run(['/bin/bash', 'deploy/compose/seed.sh'], cwd=ROOT,
                            env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


@pytest.mark.parametrize('setting', [None, '0'])
def test_default_seed_only_bootstraps_admin_without_demo_storage_or_idp_login(tmp_path, setting):
    result, calls = run_seed(tmp_path, setting)
    assert result.returncode == 0, result.stderr
    python = [c for c in calls if c['tool']=='python3']
    assert len(python) == 1 and python[0]['args']==['deploy/compose/bootstrap_admin.py']
    assert python[0]['bootstrap_api'] == 'https://seed.test:9090/api'
    assert python[0]['ca'] == '/test-ca/rootCA.pem'
    assert not any(c['tool'] in ('bash', 'curl', 'docker') for c in calls)
    assert 'demo seed skipped' in result.stdout


def test_examples_are_only_seeded_after_explicit_opt_in(tmp_path):
    result, calls = run_seed(tmp_path, '1')
    assert result.returncode == 0, result.stderr
    assert any(c['tool']=='bash' and c['args']==['deploy/compose/example-storage/setup.sh'] for c in calls)
    python = [c['args'] for c in calls if c['tool']=='python3']
    assert ['deploy/compose/seed_api.py', '--check-ready'] in python
    assert python[-1] == ['deploy/compose/seed_rbac.py']
    assert ['deploy/compose/bootstrap_admin.py'] not in python


def test_invalid_example_storage_flag_fails_before_any_external_command(tmp_path):
    result, calls = run_seed(tmp_path, 'false')
    assert result.returncode == 2 and not calls
    assert 'EXAMPLE_STORAGE must be 0' in result.stderr
