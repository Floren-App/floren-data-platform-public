"""Strimzi v1 resources. Only the layer reconciler writes these objects."""
import ipaddress
from sqlalchemy import select

from .models import ComputePool, Resource
from .layer_manifests import COMPONENT, LAYER, OWNER, ProvisioningError, WorkerPlan

CLUSTER = 'fdp-kafka'
POLICY_SECRET = 'fdp-kafka-policy'


def broker_plan(db, spec, layer_id):
    if not spec.kafka.running:
        return None
    pool = db.scalar(select(ComputePool).where(ComputePool.name == spec.kafka.warehouse))
    if not pool or pool.memory_gb < 1:
        raise ProvisioningError('KafkaWarehouseMissing', 'Kafka needs an existing warehouse with at least 1 GB per broker')
    if pool.layer_ids and layer_id not in pool.layer_ids:
        raise ProvisioningError('KafkaWarehouseLayer', 'The Kafka warehouse is not available in this layer')
    if pool.workload_kinds and 'kafka' not in pool.workload_kinds:
        raise ProvisioningError('KafkaWarehouseKind', 'The warehouse must allow Kafka broker workloads')
    from .capacity import trino_warehouses
    if pool.name == spec.trino.workers.warehouse or pool.name in trino_warehouses(db):
        raise ProvisioningError('KafkaWarehouseReserved', 'Choose a separate warehouse; Trino worker warehouses are reserved for Trino')
    return WorkerPlan(pool.name, pool.cpu, pool.memory_gb, spec.kafka.replicas)


def manifests(layer_id, namespace, spec, config, plan):
    if not spec.kafka.running:
        return []
    if plan is None:
        raise ProvisioningError('KafkaWarehouseMissing', 'Resolve the Kafka broker warehouse before provisioning')
    if len(config.kafka_policy_key) < 32:
        raise ProvisioningError('KafkaPolicyKeyMissing', 'Configure the controller Kafka policy key through the installation Secret')
    try:
        api_networks = [ipaddress.ip_network(value, strict=True) for value in config.kafka_api_server_cidrs]
    except ValueError:
        raise ProvisioningError('KafkaApiBoundaryMissing', 'Kafka API server addresses must be valid host CIDRs') from None
    if not api_networks or any(n.prefixlen != n.max_prefixlen for n in api_networks):
        raise ProvisioningError('KafkaApiBoundaryMissing', 'Set layerController.network.kafkaApiServerCidrs to the exact Kubernetes API service and endpoint IPs (/32 or /128)')
    k = spec.kafka
    labels = {OWNER:config.installation, LAYER:layer_id, COMPONENT:'kafka', 'app.kubernetes.io/managed-by':'fdp-layer-controller'}
    def obj(kind, name, body, api='kafka.strimzi.io/v1'):
        return {'apiVersion':api, 'kind':kind,
                'metadata':{'name':name, 'namespace':namespace, 'labels':dict(labels)}, **body}
    root = f'http://fdp-control-plane.{config.platform_namespace}.svc:8000/internal/kafka'
    jaas = ('org.apache.kafka.common.security.oauthbearer.OAuthBearerLoginModule required '
            'oauth.valid.issuer.uri="fdp-kafka" '
            f'oauth.jwks.endpoint.uri="{root}/jwks" '
            f'oauth.check.audience="true" oauth.client.id="fdp-kafka:{layer_id}" '
            'oauth.username.claim="sub";')
    pod_labels = {OWNER:config.installation, LAYER:layer_id, COMPONENT:'kafka', 'fdp.floren.app/role':'kafka'}
    security = {'runAsNonRoot':True, 'allowPrivilegeEscalation':False,
                'capabilities':{'drop':['ALL']}, 'seccompProfile':{'type':'RuntimeDefault'}}
    listener = {'name':'clients', 'port':9093, 'type':'internal', 'tls':True,
        'authentication':{'type':'custom', 'sasl':True, 'listenerConfig':{
            'sasl.enabled.mechanisms':'OAUTHBEARER',
            'oauthbearer.sasl.server.callback.handler.class':'io.strimzi.kafka.oauth.server.JaasServerOauthValidatorCallbackHandler',
            'oauthbearer.sasl.login.callback.handler.class':'app.floren.kafka.FdpListenerLogin',
            'oauthbearer.sasl.jaas.config':jaas,
            'connections.max.reauth.ms':k.token_ttl_seconds*1000}},
        'configuration':{'brokerCertChainAndKey':{'secretName':k.tls.secret_name,
                                               'certificate':'tls.crt','key':'tls.key'}}}
    docs = [obj('KafkaNodePool', 'brokers', {'metadata':{'name':'brokers','namespace':namespace,
                'labels':{**labels,'strimzi.io/cluster':CLUSTER}}, 'spec':{
        'replicas':k.replicas, 'roles':['broker','controller'],
        'resources':{'requests':{'cpu':str(plan.cpu),'memory':f'{int(plan.memory_gb*1024)}Mi'},
                     'limits':{'cpu':str(plan.cpu),'memory':f'{int(plan.memory_gb*1024)}Mi'}},
        'storage':{'type':'persistent-claim','size':k.size,'deleteClaim':False,
                   **({'class':k.storage_class} if k.storage_class else {})}}}),
        obj('Kafka', CLUSTER, {'spec':{'kafka':{'version':k.version, 'image':k.image,
            'listeners':[listener],
            'authorization':{'type':'custom','authorizerClass':'app.floren.kafka.FdpAuthorizer','supportsAdminApi':False},
            'config':{'auto.create.topics.enable':False, 'default.replication.factor':min(3,k.replicas),
                'principal.builder.class':'io.strimzi.kafka.oauth.server.OAuthKafkaPrincipalBuilder',
                'min.insync.replicas':1 if k.replicas==1 else 2,
                'offsets.topic.replication.factor':min(3,k.replicas),
                'transaction.state.log.replication.factor':min(3,k.replicas),
                'transaction.state.log.min.isr':1 if k.replicas==1 else 2,
                'fdp.policy.url':root+'/layers/'+layer_id+'/allow',
                'fdp.policy.token.file':'/mnt/fdp-policy/token'},
            'template':{'podSet':{'metadata':{'labels':labels}},
                'pod':{'metadata':{'labels':pod_labels},
                **({'imagePullSecrets':[{'name':config.image_pull_secret}]} if config.image_pull_secret else {}),
                'volumes':[{'name':'fdp-policy','secret':{'secretName':POLICY_SECRET}}]},
                'kafkaContainer':{'securityContext':security,
                    'volumeMounts':[{'name':'fdp-policy','mountPath':'/mnt/fdp-policy','readOnly':True}]}},
        }, 'entityOperator':{'topicOperator':{'resources':{
            'requests':{'cpu':'500m','memory':'512Mi'},'limits':{'cpu':'500m','memory':'512Mi'}}}, 'template':{
            'deployment':{'metadata':{'labels':labels}},
            'pod':{'metadata':{'labels':{**pod_labels,'fdp.floren.app/role':'kafka-operator'}}}}}}})]
    if k.tls.mode == 'cert_manager':
        sans = [f'{CLUSTER}-kafka-bootstrap.{namespace}.svc', f'{CLUSTER}-kafka-bootstrap.{namespace}.svc.cluster.local',
                f'*.{CLUSTER}-kafka-brokers.{namespace}.svc', f'*.{CLUSTER}-kafka-brokers.{namespace}.svc.cluster.local']
        docs.insert(0, obj('Certificate', 'fdp-kafka', {'spec':{'secretName':k.tls.secret_name,
            'issuerRef':k.tls.issuer.model_dump(), 'dnsNames':sans,
            'duration':f'{k.tls.duration_hours}h', 'renewBefore':f'{k.tls.renew_before_hours}h',
            # Kafka's PEM keystore reads PKCS#8 only (cert-manager's default for ECDSA is SEC1).
            'privateKey':{'algorithm':'ECDSA','size':256,'encoding':'PKCS8','rotationPolicy':'Always'}}}, 'cert-manager.io/v1'))
    # Retiring pauses Strimzi reconciliation first; brokers must be stopped explicitly
    # by the operator because an empty KRaft quorum is not a valid normal desired state.
    for document in docs:
        if document['kind'] == 'Kafka':
            document['metadata']['annotations'] = {'strimzi.io/pause-reconciliation':'true' if spec.retired else 'false'}
    clients = {'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':namespace}},
               'podSelector':{'matchExpressions':[{'key':'fdp.floren.app/role','operator':'In',
                   'values':['workspace','job','compute','app','stream','workload']}]}}
    # Strimzi replaces app.kubernetes.io/* labels on its pods: select brokers by FDP's role label.
    brokers = {'podSelector':{'matchLabels':{OWNER:config.installation,LAYER:layer_id,'fdp.floren.app/role':'kafka'}}}
    operator = {'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':config.platform_namespace}},
                'podSelector':{'matchLabels':{'strimzi.io/kind':'cluster-operator'}}}
    controller = {'namespaceSelector':{'matchLabels':{'kubernetes.io/metadata.name':config.platform_namespace}},
                  'podSelector':{'matchLabels':{'app.kubernetes.io/name':'fdp-layer-controller'}}}
    listener['networkPolicyPeers'] = [clients,controller]
    docs.append(obj('NetworkPolicy', 'kafka-internal', {'spec':{
        'podSelector':brokers['podSelector'],
        'policyTypes':['Ingress','Egress'],
        'ingress':[{'from':[clients,controller],'ports':[{'protocol':'TCP','port':9093}]},
                   {'from':[brokers,operator],'ports':[{'protocol':'TCP','port':p} for p in (9090,9091,9093,8443,8080)]}],
        'egress':[{'to':[brokers],'ports':[{'protocol':'TCP','port':p} for p in (9090,9091,9093,8443,8080)]}]}},
        'networking.k8s.io/v1'))
    docs.append(obj('NetworkPolicy','kafka-clients',{'spec':{
        'podSelector':clients['podSelector'], 'policyTypes':['Egress'],
        'egress':[{'to':[brokers],'ports':[{'protocol':'TCP','port':9093}]}]}}, 'networking.k8s.io/v1'))
    # The topic operator talks to the brokers over the internal TLS listener only.
    docs.append(obj('NetworkPolicy','kafka-topic-operator',{'spec':{
        'podSelector':{'matchLabels':{OWNER:config.installation,LAYER:layer_id,'fdp.floren.app/role':'kafka-operator'}},
        'policyTypes':['Egress'], 'egress':[{'to':[brokers],'ports':[{'protocol':'TCP','port':9091}]}]}},
        'networking.k8s.io/v1'))
    # Brokers read their certificates through Strimzi's Kubernetes config provider; the topic operator
    # reconciles KafkaTopics. Only these two reach the API, and only its exact addresses.
    docs.append(obj('NetworkPolicy','kafka-topic-operator-api',{'spec':{
        'podSelector':{'matchLabels':{OWNER:config.installation,LAYER:layer_id},'matchExpressions':[
            {'key':'fdp.floren.app/role','operator':'In','values':['kafka','kafka-operator']}]},
        'policyTypes':['Egress'], 'egress':[{'to':[{'ipBlock':{'cidr':str(n)}} for n in api_networks],
            'ports':[{'protocol':'TCP','port':443},{'protocol':'TCP','port':6443}]}]}},'networking.k8s.io/v1'))
    if config.cilium:
        # Cilium matches the API server only as its kube-apiserver entity, not by ipBlock (unless
        # policy-cidr-match-mode includes nodes): the same pods, the same ports, nothing else.
        docs.append(obj('CiliumNetworkPolicy','kafka-api-server',{'spec':{
            'endpointSelector':docs[-1]['spec']['podSelector'],
            'egress':[{'toEntities':['kube-apiserver'],'toPorts':[{'ports':[
                {'port':'443','protocol':'TCP'},{'port':'6443','protocol':'TCP'}]}]}]}},'cilium.io/v2'))
    return docs


def topic_manifest(resource, namespace, installation):
    c = resource.config
    return {'apiVersion':'kafka.strimzi.io/v1', 'kind':'KafkaTopic',
        'metadata':{'name':'topic-'+resource.id, 'namespace':namespace,
            'labels':{OWNER:installation,LAYER:c['layer_id'],'strimzi.io/cluster':CLUSTER}},
        'spec':{'topicName':resource.name, 'partitions':c['partitions'], 'replicas':c['replication_factor'],
                'config':{'retention.ms':c['retention_ms'],'cleanup.policy':c['cleanup_policy']}}}


def ensure_cluster(db, layer):
    """Idempotent catalog identity for every layer; never grants data to ordinary groups."""
    import hashlib
    from .models import RoleAssignment
    from .ownership import GLOBAL_ADMIN, ensure_global_admin
    existing = db.scalar(select(Resource).where(Resource.kind == 'kafka', Resource.object_id == layer.id))
    if existing:
        if existing.config.get('deleted'):
            existing.config = {**existing.config, 'deleted':False}
            db.flush()
        return existing
    ensure_global_admin(db)
    ident = hashlib.sha256(('layer-kafka:' + layer.id).encode()).hexdigest()[:12]
    values = dict(id=ident, kind='kafka', name=layer.name+' Kafka', object_id=layer.id,
                  parent_id=layer.id, config={'layer_id':layer.id, 'managed':True, 'owner_group_id':GLOBAL_ADMIN})
    if db.bind.dialect.name in ('sqlite', 'postgresql'):
        if db.bind.dialect.name == 'sqlite':
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert
        db.execute(insert(Resource).values(**values).on_conflict_do_nothing(index_elements=['id']))
        grant_id = hashlib.sha256(('layer-kafka-owner:' + layer.id).encode()).hexdigest()[:12]
        db.execute(insert(RoleAssignment).values(id=grant_id, resource_id=ident, app_group_id=GLOBAL_ADMIN, role='Owner')
                   .on_conflict_do_nothing(index_elements=['id']))
    else:
        db.add(Resource(**values)); db.flush()
        db.add(RoleAssignment(resource_id=ident, app_group_id=GLOBAL_ADMIN, role='Owner'))
    db.flush()
    return db.get(Resource, ident)
