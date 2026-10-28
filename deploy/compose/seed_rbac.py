"""Explicit, idempotent RBAC seed for the disposable Compose example, not production.

Storage is registered separately (make storage-example, or your own integrations); this script
only adds identities, groups, namespaces and role assignments on top of it."""
import sys

from seed_api import request, wait_ready


def source_schema(source, name, **config):
    source_id = next(s['id'] for s in request('GET', '/v1/admin/datasources') if s['name'] == source)
    existing = next((r for r in request('GET', '/v1/rbac/resources')
                     if r['kind'] == 'schema' and r['object_id'] == source_id and r['name'] == name), None)
    return existing or request('POST', f'/v1/rbac/sources/{source}/schemas', {'name': name, **config})


wait_ready()
if '--check-ready' in sys.argv:
    sys.exit(0)


def directory(collection, name, **body):
    key = 'userName' if collection == 'Users' else 'displayName'
    existing = next((p for p in request('GET', '/scim/v2/' + collection, scim=True)['Resources'] if p.get(key) == name), None)
    payload = {key: name, 'externalId': 'local-' + name, **body}
    if existing:
        return request('PUT', f"/scim/v2/{collection}/{existing['id']}", payload, scim=True)
    return request('POST', '/scim/v2/' + collection, payload, scim=True)


integrations = {s['name'] for s in request('GET', '/v1/admin/datasources')}
missing = {'finance-dw', 'lake-files', 'lake'} - integrations
if missing:
    sys.exit('Seed needs the storage integrations ' + ', '.join(sorted(missing)) + '. Register the local example '
             'storage with `make storage-example` (or create integrations with these names for your own storage '
             'under Administration → Storage integrations), then run the seed again.')
admin = directory('Users', 'admin', active=True, roles=[{'value': 'fdp-admin'}])
ana = directory('Users', 'ana', active=True, roles=[{'value': 'fdp-admin'}])
bob = directory('Users', 'bob', active=True)
finance = directory('Groups', 'finance', members=[{'value': ana['id']}])
marketing = directory('Groups', 'marketing', members=[{'value': bob['id']}])
request('PUT', '/v1/admin/repos/analytics', {'name': 'analytics', 'url': 'http://gitea:3000/ana/analytics.git',
    'default_branch': 'main', 'description': 'Example programs',
    'secret': {'username': 'fdp', 'token': 'fdp123'}})
request('PUT', '/v1/admin/service-accounts/finance-etl', {
    'description': 'Finance pipelines', 'groups': [], 'allowed_groups': [], 'disabled': False})
resources = request('GET', '/v1/rbac/resources')
sa_resource = next(r for r in resources if r['config'].get('service_account') and r['name'] == 'Service account: finance-etl')
sa_pid = sa_resource['config']['principal_id']


def app_group(name, principal_ids):
    existing = next((g for g in request('GET', '/v1/rbac/groups') if g['name'] == name), None)
    group = existing or request('POST', '/v1/rbac/groups', {'name': name, 'description': 'Local example', 'owners': [admin['id']]})
    for pid in principal_ids:
        request('POST', f"/v1/rbac/groups/{group['id']}/bindings", {'principal_id': pid})
    return group['id']


def assign(resource_id, group, role):
    request('PUT', f'/v1/rbac/resources/{resource_id}/assignments', {'app_group_id': group, 'role': role})


readers = app_group('Finance readers', [finance['id'], sa_pid])
developers = app_group('Finance developers', [finance['id']])
production = app_group('Finance production', [sa_pid])
operators = app_group('Finance service owners', [finance['id']])
viewers = app_group('Example viewers', [marketing['id']])
assign('workloads', developers, 'Owner')
assign('workloads', viewers, 'Executor')
assign('workloads', production, 'Executor')
assign(sa_resource['id'], operators, 'Owner')
for name in ('public', 'analytics', 'dbt_ana'):
    schema = source_schema('finance-dw', name)
    request('POST', f"/v1/rbac/resources/{schema['id']}/provision")
    if name == 'public':
        assign(schema['id'], readers, 'Reader')
    elif name == 'dbt_ana':
        assign(schema['id'], developers, 'Admin')
    else:
        assign(schema['id'], production, 'Admin')
files = source_schema('lake-files', 'finance-files', prefix='')
request('POST', f"/v1/rbac/resources/{files['id']}/provision")
assign(files['id'], developers, 'Contributor')
assign(files['id'], production, 'Contributor')
for name in ('finance', 'analytics', 'dbt_ana'):
    schema = source_schema('lake', name)
    request('POST', f"/v1/rbac/resources/{schema['id']}/provision")
    request('POST', '/v1/sql/query', {'source': 'lake', 'sql': f'CREATE SCHEMA IF NOT EXISTS lake.{name}'})
    assign(schema['id'], readers, 'Reader')
    if name in ('finance', 'dbt_ana'):
        assign(schema['id'], developers, 'Admin')
    assign(schema['id'], production, 'Admin')
print('Registered SCIM identities, PostgreSQL/Iceberg/S3 namespaces and roles, finance-etl, and folders.')
