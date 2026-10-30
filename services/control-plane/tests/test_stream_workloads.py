"""Continuous workload deployment, managed identity, capacity and observed lineage."""
import json

from sqlalchemy import select

from app import deps, kafka_access, layers
from app.db import SessionLocal
from app.models import KafkaSession, Workload, CatalogAsset, LineageEdge
from .conftest import as_
from .test_deploying import repo, deploy, push, git
from .test_kafka_access import topic
from .test_platform import wl_headers


def test_stream_deploys_under_managed_identity_with_readiness_but_no_public_app_route(client, repo, topic):
    def edit(path):
        config=json.loads((path/'fdp.json').read_text())
        config['programs']['events']={'kind':'stream','entrypoint':'events.py','warehouse':'small'}
        (path/'fdp.json').write_text(json.dumps(config))
        (path/'events.py').write_text('from fdp.streaming import ready\nready()\n')
        git('add','.',cwd=path)
    push(repo['work'],'Add continuous stream',edit)
    assert deploy(client,repo,user='admin',program='events',slug='events',layer_id=layers.NON_PROD).status_code==422
    response=deploy(client,repo,user='admin',program='events',slug='events',execution_mode='fixed',layer_id=layers.NON_PROD)
    assert response.status_code==201,response.text
    pub=response.json()
    assert pub['kind']=='stream' and pub['status']=='live' and pub['url'] is None
    assert pub['identity'].startswith('service:')
    assert pub['execution_state']=='running'
    orch=deps.get_orchestrator()
    spec=orch.executions['pub-'+pub['id']+'-'+pub['deployed_sha'][:7]]
    assert 'python -m fdp.streaming -- python events.py' in spec.command[2]
    assert 'FDP_EXECUTION_MODE' not in spec.env
    assert client.post('/v1/workloads/'+pub['id']+'/runs',headers=as_('admin'),json={}).status_code==409
    assert client.post('/v1/workloads/'+pub['id']+'/open',headers=as_('admin')).status_code==409
    # A stream uses a direct topic grant, not the publisher's Global Admin privileges.
    headers=wl_headers(spec)
    assert client.post('/v1/kafka/topics/'+topic['id']+'/session',headers=headers,json={'mode':'produce'}).status_code==403
    with SessionLocal() as db:
        record=db.get(Workload,pub['id'])
        pid=record.service_principal_id
    granted=client.put('/v1/rbac/resources/'+topic['id']+'/assignments',headers=as_('admin'),
        json={'principal_id':pid,'role':'Contributor'})
    assert granted.status_code==200,granted.text
    issued=client.post('/v1/kafka/topics/'+topic['id']+'/session',headers=headers,json={'mode':'produce'})
    assert issued.status_code==200,issued.text
    observed=client.post('/v1/kafka/topics/'+topic['id']+'/observations',headers=headers,json={'mode':'produce'})
    assert observed.status_code==201,observed.text
    with SessionLocal() as db:
        row=db.scalar(select(KafkaSession))
        assert row.principal_id==pid and kafka_access.session_active(db,row)
        asset=db.scalar(select(CatalogAsset).where(CatalogAsset.resource_id==topic['id']))
        assert asset.last_success_at is not None
        assert db.scalar(select(LineageEdge).where(LineageEdge.target_id==asset.id,LineageEdge.origin=='kafka_runtime'))
    overview=client.get('/v1/compute/warehouses',headers=as_('admin')).json()
    assert any(o['kind']=='stream' and o['workload_id']==pub['id'] for w in overview for o in w['occupants'])
    stopped=client.delete('/v1/workloads/'+pub['id'],headers=as_('admin'))
    assert stopped.status_code==204,stopped.text
    with SessionLocal() as db:
        assert not kafka_access.session_active(db,db.scalar(select(KafkaSession)))
