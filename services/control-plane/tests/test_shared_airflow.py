"""Shared operational metadata must preserve per-publication and layer isolation."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import airflow_db, airflow_history, deps, layers
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models import Workload
from .conftest import as_
from .test_platform import platform_repo  # noqa: F401
from .test_deploying import deploy, repo  # noqa: F401
from .test_layer_airflow import ready_layer, distributed  # noqa: F401
from .test_airflow_history import metadata, register


def test_split_credentials_are_escaped_and_external_tls_options_are_preserved(monkeypatch):
    settings = Settings(airflow_metadata_host='db.system.svc', airflow_metadata_password='p@ss:/word')
    from sqlalchemy.engine import make_url
    assert make_url(settings.airflow_metadata_url).password == 'p@ss:/word'
    monkeypatch.setattr(get_settings(), 'airflow_metadata_url',
        'postgresql://provisioner:ADMIN@db:5432/postgres?sslmode=verify-full&sslrootcert=/trust/ca.pem')
    url = airflow_db.shared_database_url('abcdef123456', {'password': 'tenant@password'})
    assert url.username == url.database == 'airflow_abcdef123456'
    assert url.password == 'tenant@password' and url.query['sslmode'] == 'verify-full'
    assert 'ADMIN' not in url.render_as_string(hide_password=False)
    with pytest.raises(airflow_db.AirflowDatabaseError):
        airflow_db.shared_database_url("x; DROP ROLE fdp", {'password': 'x'})


@pytest.fixture
def shared_database(monkeypatch):
    monkeypatch.setattr(get_settings(), 'airflow_metadata_url', 'postgresql://provisioner:ADMIN@db/postgres')
    calls = []
    def provision(store, pid):
        keys = airflow_db.deployment_keys(store, pid)
        keys['metadata_backend'] = 'shared-postgres'
        store.put(airflow_db.keys_path(pid), keys)
        calls.append(pid)
        return airflow_db.shared_database_url(pid, keys).render_as_string(hide_password=False)
    monkeypatch.setattr(airflow_db, 'ensure_database', provision)
    return calls


def test_standalone_layer_airflow_uses_private_shared_database_without_postgres_pod(
        client, platform_repo, ready_layer, shared_database):  # noqa: F811
    pub = deploy(client, platform_repo, program='pipelines', slug='pipelines').json()
    assert pub['status'] == 'live', pub
    spec = deps.get_orchestrator().executions[f"pub-{pub['id']}-{pub['deployed_sha'][:7]}"]
    assert shared_database == [pub['id']]
    assert spec.labels['fdp.floren.app/airflow-role'] == 'standalone'
    assert spec.placement.namespace.endswith(layers.NON_PROD)
    assert f"/airflow_{pub['id']}" in spec.secret_env['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN']
    assert 'ADMIN' not in str(spec.secret_env) and 'SQL_ALCHEMY_CONN' not in str(spec.env)
    assert spec.env['AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE'] == '2'
    assert not any(n.endswith('-metadata') for n in deps.get_orchestrator().executions)


def test_distributed_layer_airflow_shares_server_but_keeps_layer_broker(
        client, platform_repo, ready_layer, shared_database):  # noqa: F811
    from .test_platform import push
    push(platform_repo['work'], 'Distributed', distributed)
    pub = deploy(client, platform_repo, program='pipelines', slug='pipelines').json()
    assert pub['status'] == 'live', pub
    specs = deps.get_orchestrator().executions
    assert any(n.endswith('-broker') for n in specs)
    assert not any(n.endswith('-metadata') for n in specs)
    web = next(s for n,s in specs.items() if n.endswith('-web'))
    assert f"/airflow_{pub['id']}" in web.secret_env['AIRFLOW__DATABASE__SQL_ALCHEMY_CONN']
    assert 'ADMIN' not in str(web.secret_env)


def test_standalone_shared_history_uses_tenant_login_and_never_the_sqlite_holder(client, repo, tmp_path, monkeypatch):  # noqa: F811
    pid, _ = register(client, repo, kind='airflow')
    deps.get_secret_store().put('airflow/'+pid, {'password':'TENANT', 'metadata_backend':'shared-postgres'})
    monkeypatch.setattr(get_settings(), 'airflow_metadata_url', 'postgresql://provisioner:ADMIN@db/postgres')
    engine,_ = metadata(tmp_path,pid)
    seen=[]
    def connect(url,**kwargs):
        seen.append((url,kwargs));return engine
    monkeypatch.setattr(airflow_history,'create_engine',connect)
    result=client.get(f'/v1/workloads/{pid}/orchestrator-runs',headers=as_('ana'))
    assert result.status_code==200,result.text
    url,options=seen[0]
    assert url.username==url.database=='airflow_'+pid and url.password=='TENANT'
    assert 'default_transaction_read_only=on' in options['connect_args']['options']
    assert 'ADMIN' not in result.text and 'TENANT' not in result.text


def test_existing_history_cannot_be_silently_discarded(client, platform_repo, monkeypatch):  # noqa: F811
    pub = deploy(client,platform_repo,program='pipelines',slug='pipelines').json()
    monkeypatch.setattr(get_settings(),'airflow_metadata_url','postgresql://provisioner:ADMIN@db/postgres')
    monkeypatch.setattr(airflow_history,'recent_runs',lambda *args,**kw:{'runs':[{'run_id':'keep-me'}]})
    def unexpected(*args):raise AssertionError('must not provision')
    monkeypatch.setattr(airflow_db,'ensure_database',unexpected)
    from app.deploying import _shared_airflow_database
    with SessionLocal() as db:
        with pytest.raises(airflow_db.AirflowDatabaseError,match='must be migrated'):
            _shared_airflow_database(db,deps.get_orchestrator(),db.get(Workload,pub['id']),deps.get_secret_store())


def test_shared_network_exception_only_selects_airflow_components():
    from app.layer_manifests import manifests
    from .test_layer_controller import WORKERS,CONFIG
    from .test_layer_infrastructure import managed
    from app.layer_infrastructure import InfrastructureSpec
    docs=manifests(layers.NON_PROD,1,InfrastructureSpec.model_validate(managed()),
        replace(CONFIG,shared_database_namespace='fdp-system-data'),workers=WORKERS)
    policy=next(d['spec'] for d in docs if d['kind']=='NetworkPolicy' and d['metadata']['name']=='airflow-shared-metadata')
    assert policy['podSelector']['matchExpressions']==[{'key':'fdp.floren.app/airflow-role','operator':'In',
        'values':['standalone','scheduler','web','worker']}]
    assert policy['egress']==[{'to':[{'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':'fdp-system-data'}},
        'podSelector':{'matchLabels':{'app.kubernetes.io/name':'fdp-metadata-db'}}}], 'ports':[{'protocol':'TCP','port':5432}]}]
