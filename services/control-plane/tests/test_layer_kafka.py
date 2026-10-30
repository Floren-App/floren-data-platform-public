"""Broker manifests against the pinned operator schema and retained-storage boundary."""
from pathlib import Path
import tarfile

import jsonschema
import pytest
import yaml

from app.layer_infrastructure import InfrastructureSpec
from app.layer_manifests import ControllerConfig, WorkerPlan, OWNER, LAYER, COMPONENT
from app.layer_kafka import manifests, topic_manifest
from app import layer_controller as controller
from app.models import Resource
from app.db import SessionLocal
from app import layers
from .test_kafka_access import topic

ID = 'abcdef123456'
CONFIG = ControllerConfig('platform', 'installation', 'fdp-layer', 'runtime', artifact_image='fdp/control-plane:1', kafka_policy_key='test-key-with-at-least-32-characters', kafka_api_server_cidrs=('10.96.0.1/32','192.0.2.1/32'))
PLAN = WorkerPlan('streaming', 1, 2, 3)


def spec(**overrides):
    return InfrastructureSpec.model_validate({'enabled':True, 'kafka':{'enabled':True,
        'warehouse':'streaming', 'replicas':3, 'image':'fdp/kafka:1',
        'tls':{'mode':'existing','secret_name':'kafka-tls'}}, **overrides})


def test_resources_validate_against_pinned_strimzi_crds():
    archive = Path(__file__).resolve().parents[3] / 'deploy/helm/fdp/charts/strimzi-kafka-operator-helm-3-chart-1.2.0.tgz'
    if not archive.exists():
        pytest.skip('Run helm dependency build deploy/helm/fdp to validate operator schemas')
    documents = manifests(ID, 'fdp-layer-'+ID, spec(), CONFIG, PLAN)
    documents.append(topic_manifest(Resource(id='123456abcdef', name='events', config={
        'layer_id':ID,'partitions':3,'replication_factor':3,'retention_ms':86400000,'cleanup_policy':'delete'}),
        'fdp-layer-'+ID, 'installation'))
    with tarfile.open(archive) as chart:
        schemas = {}
        for name in chart.getnames():
            if '/crds/' in name and name.endswith('.yaml'):
                crd = yaml.safe_load(chart.extractfile(name))
                version = next((v for v in crd['spec']['versions'] if v['name'] == 'v1'), None)
                if version:
                    schemas[crd['spec']['names']['kind']] = version['schema']['openAPIV3Schema']
    def strict(schema):
        if isinstance(schema, dict):
            if schema.get('type') == 'object' and 'properties' in schema and not schema.get('x-kubernetes-preserve-unknown-fields'):
                schema.setdefault('additionalProperties',False)
            for value in schema.values():
                strict(value)
        elif isinstance(schema,list):
            for value in schema:
                strict(value)
    for document in documents:
        if document['kind'] in schemas:
            schema = schemas[document['kind']]
            # Kubernetes standard metadata is intentionally not described in CRD schemas.
            schema['properties']['metadata'] = {'type':'object'}
            strict(schema)
            errors = list(jsonschema.Draft7Validator(schema).iter_errors(document))
            assert not errors, [(list(e.path), e.message) for e in errors]
    broker = next(d for d in documents if d['kind']=='Kafka')['spec']['kafka']
    assert broker['authorization']['supportsAdminApi'] is False
    assert 'OAuthKafkaPrincipalBuilder' in broker['config']['principal.builder.class']
    assert broker['listeners'][0]['networkPolicyPeers']
    assert next(d for d in documents if d['kind']=='KafkaNodePool')['spec']['storage']['deleteClaim'] is False


def test_network_policies_select_brokers_by_the_labels_strimzi_keeps():
    """Strimzi replaces app.kubernetes.io/* labels on broker pods; found on the live cluster."""
    documents = manifests(ID, 'fdp-layer-'+ID, spec(), CONFIG, PLAN)
    kafka = next(d for d in documents if d['kind']=='Kafka')['spec']['kafka']
    pod = kafka['template']['pod']['metadata']['labels']
    live = {k:v for k,v in pod.items() if not k.startswith('app.kubernetes.io/')}
    policies = {d['metadata']['name']:d['spec'] for d in documents if d['kind']=='NetworkPolicy'}
    def selects(selector, labels):
        return (all(labels.get(k)==v for k,v in selector.get('matchLabels',{}).items()) and
                all(labels.get(e['key']) in e['values'] for e in selector.get('matchExpressions',[])))
    assert selects(policies['kafka-internal']['podSelector'], live)
    # Brokers read their certificate Secrets from the API at startup (Strimzi config provider).
    assert selects(policies['kafka-topic-operator-api']['podSelector'], live)
    assert not selects(policies['kafka-topic-operator-api']['podSelector'], {**live,'fdp.floren.app/role':'stream'})


def test_cilium_gets_the_api_server_entity_for_the_same_pods_only():
    """Cilium does not match the API server by ipBlock; found on the live cluster."""
    from dataclasses import replace
    plain = manifests(ID, 'fdp-layer-'+ID, spec(), CONFIG, PLAN)
    assert not [d for d in plain if d['kind']=='CiliumNetworkPolicy']
    documents = manifests(ID, 'fdp-layer-'+ID, spec(), replace(CONFIG, cilium=True), PLAN)
    [cilium] = [d for d in documents if d['kind']=='CiliumNetworkPolicy']
    api = next(d for d in documents if d['metadata']['name']=='kafka-topic-operator-api')
    assert cilium['spec']['endpointSelector'] == api['spec']['podSelector']
    assert cilium['spec']['egress'] == [{'toEntities':['kube-apiserver'],'toPorts':[{'ports':[
        {'port':'443','protocol':'TCP'},{'port':'6443','protocol':'TCP'}]}]}]
    assert controller.RESOURCES['CiliumNetworkPolicy'][0] == '/apis/cilium.io/v2'


def test_cert_manager_key_is_pkcs8_for_kafka():
    """Kafka's PEM keystore cannot read SEC1 EC keys; found on the live cluster."""
    documents = manifests(ID, 'fdp-layer-'+ID, spec(kafka={'enabled':True,'warehouse':'streaming','replicas':3,
        'image':'fdp/kafka:1','tls':{'mode':'cert_manager','secret_name':'kafka-tls',
        'issuer':{'name':'layers','kind':'ClusterIssuer'}}}), CONFIG, PLAN)
    certificate = next(d for d in documents if d['kind']=='Certificate')
    assert certificate['spec']['privateKey']['encoding'] == 'PKCS8'


def test_opt_in_requires_managed_tls_warehouse_and_authorizer():
    from pydantic import ValidationError
    for patch in ({'enabled':False}, {'kafka':{'enabled':True}},
                  {'kafka':{'enabled':True,'warehouse':'streaming','image':'fdp/kafka:latest'}}):
        with pytest.raises(ValidationError):
            spec(**patch)
    assert manifests(ID,'ns',InfrastructureSpec(),CONFIG,None)==[]


def test_missing_operator_is_reported_before_creating_layer_resources(monkeypatch):
    from kubernetes.client.exceptions import ApiException
    outcomes=[]
    class Resources:
        def request(self,*args):raise ApiException(status=404)
        def apply(self,*args):pytest.fail('Do not create a namespace/compute before checking required Kafka APIs')
    monkeypatch.setattr(controller,'finish',lambda *args:outcomes.append(args))
    claim=controller.Claim('operation',ID,1,'token',spec(),workers=WorkerPlan('trino',1,4,1),kafka_workers=PLAN)
    controller.reconcile(claim,Resources(),CONFIG)
    assert outcomes[0][1:3]==('Failed','StrimziUnavailable')


def test_retirement_waits_for_operator_pause_then_stops_only_owned_compute():
    class Api:
        paused = False
        changes = []
        def call_api(self,path,method,**kwargs):
            if method=='GET':
                return {'items':[{'metadata':{'name':'owned','resourceVersion':'7','labels':{
                    OWNER:'installation',LAYER:ID,COMPONENT:'kafka'}}}]}
            self.changes.append((path,kwargs['body']))
    class Resources:
        api=Api()
        def request(self,*args):
            return {'status':{'conditions':[{'type':'ReconciliationPaused','status':'True'}] if self.api.paused else []}}
    resources=Resources()
    claim=type('Claim',(),{'layer_id':ID})()
    assert not controller.retire_kafka(claim,resources,'ns',CONFIG)
    assert not resources.api.changes
    resources.api.paused=True
    assert controller.retire_kafka(claim,resources,'ns',CONFIG)
    assert len(resources.api.changes)==2
    patches=[patch for _,patch in resources.api.changes]
    assert all(p[0]=={'op':'test','path':'/metadata/resourceVersion','value':'7'} for p in patches)
    assert {p[1]['path'] for p in patches}=={'/spec/pods','/spec/replicas'}


def test_topic_reconciliation_waits_for_current_generation_and_finalized_deletion(client,topic,monkeypatch):
    from kubernetes.client.exceptions import ApiException
    monkeypatch.setattr(controller,'renew',lambda *args,**kwargs:None)
    class Resources:
        observed=1
        exists=True
        def apply(self,document):
            return {**document,'metadata':{**document['metadata'],'generation':2},
                    'status':{'observedGeneration':self.observed,'conditions':[{'type':'Ready','status':'True'}]}}
        def request(self,method,path,body=None):
            if not self.exists:raise ApiException(status=404)
            if method=='GET':return {'metadata':{'uid':'topic-uid','labels':{OWNER:'installation',LAYER:layers.NON_PROD}}}
            assert method=='DELETE' and body['preconditions']=={'uid':'topic-uid'}
            self.exists=False
    resources=Resources();claim=type('Claim',(),{'layer_id':layers.NON_PROD})()
    def reconcile():
        controller.reconcile_kafka_topics(claim,resources,'ns',CONFIG,SessionLocal)
        with SessionLocal() as db:return db.get(Resource,topic['id']).config['phase']
    assert reconcile()=='Pending'
    resources.observed=2
    assert reconcile()=='Ready'
    with SessionLocal() as db:
        item=db.get(Resource,topic['id']);item.config={**item.config,'deleted':True};db.commit()
    assert reconcile()=='Deleting'
    assert reconcile()=='Deleted'
