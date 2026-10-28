"""S3-compatible prefix policies and STS credentials; SDK checks are not the boundary."""
import json
import os
from datetime import datetime, timezone
from fastapi import HTTPException
from .config import get_settings


def prefix_path(base, relative):
    parts = str(base or '').strip('/').split('/') + str(relative or '').strip('/').split('/')
    if any(p in ('.', '..') or any(c in p for c in ('*', '?', '$', '{', '}')) for p in parts):
        raise HTTPException(422, 'storage prefixes cannot contain traversal or policy wildcards')
    return '/'.join(p for p in parts if p)


def session_policy(source, schemas, scope):
    bucket = source.config.get('bucket')
    if not bucket or any(c in bucket for c in ('/', '*', '?', '$', '{', '}')):
        raise HTTPException(422, 'configure a literal bucket name')
    statements = []
    for name, role in scope.items():
        resource = schemas[name]
        prefix = prefix_path(source.config.get('prefix', ''), resource.config.get('prefix', name))
        actions = ['s3:GetObject', 's3:GetObjectVersion']
        if role != 'Reader':
            actions += ['s3:PutObject', 's3:DeleteObject', 's3:AbortMultipartUpload', 's3:ListMultipartUploadParts']
        statements += [
            {'Effect': 'Allow', 'Action': ['s3:GetBucketLocation'], 'Resource': [f'arn:aws:s3:::{bucket}']},
            {'Effect': 'Allow', 'Action': ['s3:ListBucket'], 'Resource': [f'arn:aws:s3:::{bucket}'],
             'Condition': {'StringLike': {'s3:prefix': [prefix, prefix + '/*'] if prefix else ['*']}}},
            {'Effect': 'Allow', 'Action': actions,
             'Resource': [f'arn:aws:s3:::{bucket}/{prefix + "/" if prefix else ""}*']},
        ]
    return {'Version': '2012-10-17', 'Statement': statements}


def credentials(source, schemas, scope, store, db=None):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
    # STS signing credentials never leave the control plane.
    if source.config.get('managed_rustfs') is True:
        from .managed_files import provisioning
        if db is None:
            raise HTTPException(409,'managed storage requires its platform context')
        base = provisioning(db,source)
    else:
        base = store.read(f'datasources/{source.name}/provisioner')
    if not base.get('access_key_id') or not base.get('secret_access_key'):
        raise HTTPException(409, 'configure separate storage STS provisioning credentials')
    cfg = source.config
    role_arn = cfg.get('role_arn')
    if not role_arn:
        raise HTTPException(409, 'configure an STS role_arn for this source')
    policy = session_policy(source, schemas, scope)
    try:
        client = boto3.client('sts', endpoint_url=cfg.get('sts_endpoint_url'),
                              verify=os.environ.get('SSL_CERT_FILE') or True,
                              region_name=cfg.get('region', 'us-east-1'),
                              aws_access_key_id=base['access_key_id'], aws_secret_access_key=base['secret_access_key'],
                              config=Config(connect_timeout=10, read_timeout=15, retries={'max_attempts': 1}))
        response = client.assume_role(RoleArn=role_arn, RoleSessionName='fdp-data',
                    DurationSeconds=max(900, min(get_settings().data_credential_ttl_seconds, 3600)),
                    Policy=json.dumps(policy, separators=(',', ':')))
        cred = response['Credentials']
    except (BotoCoreError, ClientError, KeyError):
        raise HTTPException(409, 'scoped storage credential issuance failed; no static credential fallback') from None
    expiry = cred['Expiration']
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
    return {'access_key_id': cred['AccessKeyId'], 'secret_access_key': cred['SecretAccessKey'],
            'session_token': cred['SessionToken'], 'expires_at': expiry.isoformat(),
            'lease_seconds': str(max(0, int((expiry - datetime.now(timezone.utc)).total_seconds())))}, {
        'schemas': scope,
        # Granted namespaces' prefixes, relative to the source prefix: lets the SDK attribute file reads and
        # writes to catalog objects automatically (lineage), with no declarations in fdp.json.
        'namespaces': {name: prefix_path('', schemas[name].config.get('prefix', name)) for name in scope}}
