"""Layer-owned RustFS file integrations. Root credentials stay in the control plane."""
import os
from fastapi import HTTPException

from .layer_infrastructure import InfrastructureSpec, namespace_for
from .layer_manifests import with_credentials
from .models import Resource


ROLE = 'arn:aws:iam::000000000000:role/fdp-layer-files'


def configuration(layer, requested):
    spec = InfrastructureSpec.model_validate((layer.config or {}).get('infrastructure', {}))
    if not spec.enabled or spec.retired or not spec.rustfs.enabled:
        raise HTTPException(409, 'managed file storage requires an active layer with RustFS enabled')
    bucket = requested.get('bucket')
    if bucket not in spec.rustfs.buckets or (spec.iceberg.enabled and bucket == spec.iceberg.bucket):
        raise HTTPException(422, 'declare a separate file bucket in this layer RustFS configuration; never use its Iceberg bucket')
    from .config import get_settings
    namespace = namespace_for(layer.id,get_settings().layer_namespace_prefix or 'fdp-layer')
    endpoint = '%s://fdp-rustfs.%s.svc:9000' % ('https' if spec.rustfs.tls.mode != 'disabled' else 'http',namespace)
    expected = {'endpoint_url':endpoint, 'sts_endpoint_url':endpoint, 'role_arn':ROLE, 'region':'us-east-1'}
    if any(requested.get(k) not in (None,'',v) for k,v in expected.items()):
        raise HTTPException(422, 'managed RustFS endpoints and role are derived from the source layer')
    return {**requested, **expected}


def provisioning(db, source):
    layer = db.get(Resource,source.config.get('layer_id'))
    if not layer or layer.kind != 'layer':
        raise HTTPException(409,'managed storage layer is missing')
    configuration(layer,source.config)  # revalidate on every issuance, including retirement
    spec = with_credentials(InfrastructureSpec.model_validate(layer.config['infrastructure']))
    from .config import get_settings
    from .deps import get_orchestrator
    namespace = namespace_for(layer.id,get_settings().layer_namespace_prefix or 'fdp-layer')
    reader = get_orchestrator().read_layer_secret
    secret = spec.rustfs.credentials_secret
    values = {key:reader(namespace,secret,item) for key,item in (
        ('access_key_id','access-key'),('secret_access_key','secret-key'))}
    if not all(values.values()):
        raise HTTPException(409,'layer RustFS credentials are not ready')
    return values
