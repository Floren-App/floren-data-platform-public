"""Native S3 policy scope, plus optional live RustFS verification."""
import os
import uuid
from types import SimpleNamespace
import pytest
from app.storage_access import session_policy, prefix_path
from app.models import DataSource, Resource
from .conftest import as_
from .rbac_helpers import grant_group
from .test_api import start_workspace, execution_token
from .test_deploying import repo, deploy  # noqa: F401


def test_storage_policy_has_scoped_paths_and_never_grants_policy_management():
    source = SimpleNamespace(config={'bucket': 'files', 'prefix': 'finance'})
    schemas = {'reports': SimpleNamespace(config={'prefix': 'reports'}), 'staging': SimpleNamespace(config={'prefix': 'staging'})}
    policy = session_policy(source, schemas, {'reports': 'Reader', 'staging': 'Contributor'})
    statements = policy['Statement']
    reads = next(x for x in statements if x['Resource'] == ['arn:aws:s3:::files/finance/reports/*'])
    writes = next(x for x in statements if x['Resource'] == ['arn:aws:s3:::files/finance/staging/*'])
    assert 's3:PutObject' not in reads['Action']
    assert 's3:PutObject' in writes['Action'] and 's3:DeleteObject' in writes['Action']
    assert all('s3:PutBucketPolicy' not in s['Action'] for s in statements)
    for invalid in ('../escape', 'finance/*', '${aws:username}', 'a?'):
        with pytest.raises(Exception):
            prefix_path('', invalid)


@pytest.mark.skipif(not os.getenv('FDP_TEST_S3_URL'), reason='requires disposable RustFS')
def test_live_rustfs_sts_reader_and_contributor(client, repo):
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
    endpoint = os.environ['FDP_TEST_S3_URL']
    base = {'access_key_id': os.environ.get('FDP_TEST_S3_ACCESS_KEY', 'rbac-test-root'),
            'secret_access_key': os.environ.get('FDP_TEST_S3_SECRET_KEY', 'rbac-test-password')}
    raw = boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                       aws_access_key_id=base['access_key_id'], aws_secret_access_key=base['secret_access_key'],
                       config=Config(s3={'addressing_style': 'path'}))
    bucket = 'rbac-' + uuid.uuid4().hex[:12]
    raw.create_bucket(Bucket=bucket)
    raw.put_object(Bucket=bucket, Key='finance/report.json', Body=b'ok')
    raw.put_object(Bucket=bucket, Key='secret/outside.json', Body=b'private')
    source = client.put('/v1/admin/datasources/files-test', headers=as_('admin'), json={
        'name': 'files-test', 'kind': 's3', 'config': {'endpoint_url': endpoint, 'sts_endpoint_url': endpoint,
        'bucket': bucket, 'prefix': '', 'role_arn': 'arn:aws:iam::123456789012:role/fdp'},
        'provisioner_secret': base})
    assert source.status_code == 200, source.text
    schema = client.post('/v1/rbac/sources/files-test/schemas', headers=as_('admin'),
                          json={'name': 'finance', 'prefix': 'finance'}).json()
    ready = client.post(f"/v1/rbac/resources/{schema['id']}/provision", headers=as_('admin'))
    assert ready.status_code == 200
    readers_gid = grant_group(client, schema['id'], 'Storage readers', 'Reader', users=['ana'])
    grant_group(client, schema['id'], 'Storage writers', 'Contributor', users=['bob'])
    def scoped(who):
        ws = start_workspace(client, who)
        _, token = execution_token(ws)
        info = client.get('/v1/connect/files-test', headers=token)
        assert info.status_code == 200, info.text
        cred = info.json()['credentials']
        assert cred['session_token']
        # Granted namespaces' prefixes let the SDK attribute file reads/writes to catalog objects automatically.
        assert info.json()['access']['namespaces'] == {'finance': 'finance'}
        return boto3.client('s3', endpoint_url=endpoint, region_name='us-east-1',
                aws_access_key_id=cred['access_key_id'], aws_secret_access_key=cred['secret_access_key'],
                aws_session_token=cred['session_token'], config=Config(s3={'addressing_style': 'path'}))
    reader = scoped('ana')
    assert reader.get_object(Bucket=bucket, Key='finance/report.json')['Body'].read() == b'ok'
    for operation in (lambda: reader.put_object(Bucket=bucket, Key='finance/no', Body=b'no'),
                      lambda: reader.get_object(Bucket=bucket, Key='secret/outside.json'),
                      lambda: reader.list_objects_v2(Bucket=bucket, Prefix='secret/')):
        with pytest.raises(ClientError) as error:
            operation()
        assert error.value.response['ResponseMetadata']['HTTPStatusCode'] == 403
    writer = scoped('bob')
    writer.put_object(Bucket=bucket, Key='finance/write', Body=b'yes')
    writer.delete_object(Bucket=bucket, Key='finance/write')
    with pytest.raises(ClientError):
        writer.put_object(Bucket=bucket, Key='secret/no', Body=b'no')

    from app.auth import User, mint_token
    from app import rbac, deps
    from app.db import SessionLocal
    workload = deploy(client, repo, program='sales-dashboard', slug='files-viewer').json()
    spec = deps.get_orchestrator().executions[f"pub-{workload['id']}-{workload['deployed_sha'][:7]}"]
    with SessionLocal() as db:
        viewer = rbac.resolve_user(db, User('ana'))
    viewer_token = mint_token('viewer', 'ana', 120, pid=viewer.principal_id, pub=workload['id'])
    bearer = {'Authorization': 'Bearer ' + spec.env['FDP_SESSION_TOKEN'], 'X-FDP-Viewer': viewer_token}
    assert client.get('/v1/connect/files-test', headers=bearer).status_code == 404  # no fixed credential path
    body = {'source': 'files-test', 'operation': 'read', 'path': 'finance/report.json'}
    result = client.post('/v1/delegated/files', headers=bearer, json=body)
    assert result.status_code == 200, result.text
    assert result.json()['data'] == 'b2s=' and 'session_token' not in result.text
    assert client.post('/v1/delegated/files', headers=bearer,
        json={**body, 'operation': 'write', 'data': 'bm8='}).status_code == 403
    client.delete(f"/v1/rbac/resources/{schema['id']}/assignments/{readers_gid}", headers=as_('admin'))
    assert client.post('/v1/delegated/files', headers=bearer, json=body).status_code == 404
