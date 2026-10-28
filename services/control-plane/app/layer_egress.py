"""Controller-owned data-source network grants, always scoped to a single layer.

CIDRs are administrator configuration, not discovered from untrusted DNS. Cluster
address ranges are installation prerequisites; ipBlock exceptions keep broad data
grants from bypassing namespace isolation. No credentials are read here.
"""
from dataclasses import dataclass
from ipaddress import collapse_addresses, ip_network
from typing import Tuple
from urllib.parse import urlsplit

from sqlalchemy import select

from .layer_infrastructure import namespace_for
from .layer_manifests import COMPONENT, LAYER, OWNER
from .models import DataSource

SOURCE_EGRESS = 'fdp.floren.app/source-egress'
# Never expose cloud instance credentials, loopback, multicast or link-local via
# a data grant, even when an administrator supplies a default route.
RESERVED = ('0.0.0.0/8', '127.0.0.0/8', '169.254.0.0/16', '224.0.0.0/4', '240.0.0.0/4',
            '::/96', '::ffff:0:0/96', '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', 'fe80::/10', 'ff00::/8')


@dataclass(frozen=True)
class NetworkBoundary:
    pod_cidrs: Tuple[str, ...] = ()
    service_cidrs: Tuple[str, ...] = ()
    node_cidrs: Tuple[str, ...] = ()
    protected_cidrs: Tuple[str, ...] = ()

    def __post_init__(self):
        for value in (*self.pod_cidrs, *self.service_cidrs, *self.node_cidrs, *self.protected_cidrs):
            ip_network(value, strict=True)

    @property
    def configured(self):
        return bool(self.pod_cidrs and self.service_cidrs and self.node_cidrs)

    @property
    def excluded(self):
        return tuple(ip_network(value) for value in (
            *self.pod_cidrs, *self.service_cidrs, *self.node_cidrs, *self.protected_cidrs, *RESERVED))


def source_label(source_id):
    return 'fdp.floren.app/source-' + source_id


def inventory(db, layer_id):
    from .layers import layer_of
    from .platform_metadata import SOURCE_ID
    return [source for source in db.scalars(select(DataSource).order_by(DataSource.id))
            if source.id != SOURCE_ID and source.kind in ('postgres', 'iceberg', 's3') and
            layer_of(db, (source.config or {}).get('layer_id')) == layer_id]


def _port(url, default):
    parsed = urlsplit(url or '')
    return parsed.port or ({'http': 80, 'https': 443}.get(parsed.scheme, default))


def ports(source):
    config = source.config or {}
    if source.kind == 'postgres':
        values = [config.get('port', 5432)]
    elif source.kind == 'iceberg':
        values = [_port(config.get('catalog_uri'), 443), _port(config.get('endpoint_url'), 443)]
    else:
        # STS credentials are acquired by the control plane. Executions need only S3.
        values = [_port(config.get('endpoint_url'), 443)]
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535 for value in values):
        raise ValueError('invalid data-source port')
    return [{'protocol': 'TCP', 'port': value} for value in sorted(set(values))]


def _peers(cidrs, boundary):
    peers = []
    for value in cidrs:
        network = ip_network(value, strict=True)
        if any(not any(ip_network(entry).version == network.version for entry in group)
               for group in (boundary.pod_cidrs, boundary.service_cidrs, boundary.node_cidrs)):
            raise ValueError('cluster boundary does not cover the target address family')
        excluded = [entry for entry in boundary.excluded if entry.version == network.version]
        if any(network.subnet_of(entry) for entry in excluded):
            raise ValueError('data target is inside a protected network')
        # Collapse nested exclusions; Kubernetes requires every exception to be a
        # strict subnet of cidr. An enclosing exclusion was rejected above.
        exceptions = list(collapse_addresses(entry for entry in excluded if entry.subnet_of(network)))
        peers.append({'ipBlock': {'cidr': str(network),
                                 **({'except': [str(entry) for entry in exceptions]} if exceptions else {})}})
    return peers


def policy(source, layer_id, config, egress):
    selector = {LAYER: layer_id}
    if source.kind in ('postgres', 'iceberg'):
        selector[COMPONENT] = 'trino'
    else:
        selector.update({'fdp.floren.app/managed': 'true', source_label(source.id): 'true'})
    return {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
        'metadata': {'name': 'data-source-' + source.id,
            'namespace': namespace_for(layer_id, config.namespace_prefix),
            'labels': {OWNER: config.installation, LAYER: layer_id, SOURCE_EGRESS: 'true',
                       'app.kubernetes.io/managed-by': 'fdp-layer-controller'}},
        'spec': {'podSelector': {'matchLabels': selector}, 'policyTypes': ['Egress'], 'egress': egress}}


def plan(sources, layer_id, config, boundary):
    """Invalid/removed grants become empty policies, not stale additive allows.

    Return sanitized problem codes separately: the controller applies the denying
    policy before reporting failure and blocking admission. No exception may leave
    a previously installed grant open.
    """
    documents, problems = [], []
    for source in sources:
        egress = []
        if source.kind == 's3' and (source.config or {}).get('managed_rustfs') is True:
            # Both sides are namespace-local. A source-specific label grants only
            # the authorized executions in this layer access to its RustFS API.
            target = {'podSelector': {'matchLabels': {LAYER:layer_id, COMPONENT:'rustfs'}}}
            document = policy(source,layer_id,config,[{'to':[target],'ports':[{'protocol':'TCP','port':9000}]}])
            documents.append(document)
            from copy import deepcopy
            incoming = deepcopy(document)
            incoming['metadata']['name'] += '-ingress'
            incoming['spec'] = {'podSelector':target['podSelector'],'policyTypes':['Ingress'],
                'ingress':[{'from':[{'podSelector':document['spec']['podSelector']}],
                            'ports':[{'protocol':'TCP','port':9000}]}]}
            documents.append(incoming)
            continue
        if source.egress:
            if not boundary.configured:
                problems.append('NetworkBoundaryMissing')
            else:
                try:
                    egress = [{'to': _peers(source.egress, boundary), 'ports': ports(source)}]
                except (ValueError, TypeError):
                    problems.append('DataEgressRejected')
        documents.append(policy(source, layer_id, config, egress))
    return documents, sorted(set(problems))


def disabled(document):
    """Tombstone an obsolete grant without deleting namespaces, volumes or policies."""
    return {'apiVersion': document['apiVersion'], 'kind': document['kind'],
        'metadata': {key: document['metadata'][key] for key in ('name', 'namespace', 'labels')},
        'spec': {'podSelector': document['spec']['podSelector'], 'policyTypes': document['spec']['policyTypes'],
                 **{key.lower():[] for key in document['spec']['policyTypes']}}}
