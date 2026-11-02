from app import rbac
from app.auth import User
from app.db import SessionLocal
from app.models import AppGroup, IdentityBinding, RoleAssignment, DirectoryPrincipal
from sqlalchemy import select
from .conftest import as_


def grant_group(client, resource_id, name, role, users=(), external_groups=(), services=()):
    # Exercise public management endpoints; seed OIDC-only identity facts through normal requests.
    for username in set(users) | {"admin"}:
        client.get('/v1/me', headers=as_(username))
    with SessionLocal() as db:
        admin = rbac.resolve_user(db, User('admin'))
        group = db.scalar(select(AppGroup).where(AppGroup.name == name))
        gid = group.id if group else None
        owner_id = admin.principal_id
    if not gid:
        response = client.post('/v1/rbac/groups', headers=as_('admin'),
                               json={'name': name, 'owners': [owner_id]})
        assert response.status_code == 201, response.text
        gid = response.json()['id']
    with SessionLocal() as db:
        ids = [p.id for p in db.scalars(select(DirectoryPrincipal)) if
               (p.kind == 'user' and p.name in users) or
               (p.kind == 'group' and p.name in external_groups) or
               (p.kind == 'service' and p.id in services)]
    for pid in ids:
        response = client.post(f'/v1/rbac/groups/{gid}/bindings', headers=as_('admin'), json={'principal_id': pid})
        assert response.status_code == 201, response.text
    response = client.put(f'/v1/rbac/resources/{resource_id}/assignments', headers=as_('admin'),
                          json={'app_group_id': gid, 'role': role})
    assert response.status_code == 200, response.text
    return gid


def integration(name, kind, secrets=None, **config):
    """A complete storage integration body; tests override only what they exercise."""
    defaults = {
        'postgres': {'host': 'warehouse', 'port': 5432, 'database': 'finance', 'catalog': name.replace('-', '_')},
        'iceberg': {'catalog': name.replace('-', '_'), 'warehouse': f's3://{name}/', 'catalog_uri': 'http://iceberg-rest:8181'},
        's3': {'bucket': name, 'role_arn': 'arn:aws:iam::123456789012:role/fdp'},
    }[kind]
    stored = {'postgres': {'provisioner_secret': {'username': 'governance_admin', 'password': 'governance'}},
              's3': {'provisioner_secret': {'access_key_id': 'fdp', 'secret_access_key': 'fdp-secret'}},
              'iceberg': {}}[kind]
    return {'name': name, 'kind': kind, 'config': {**defaults, **config}, **stored, **(secrets or {})}


def select_group(client, username, group_id):
    response = client.put('/v1/me/active-group', headers=as_(username), json={'group_id': group_id})
    assert response.status_code == 200, response.text


def workspace_permissions(client,group_id):
    for namespace in ('workspaces','runs','artifacts','logs'):
        response=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
            'target_id':'workspaces','namespace':namespace,'layer_id':'*',
            'app_group_id':group_id,'role':'Admin'})
        assert response.status_code==201,response.text


def secret_permissions(client,username,scope='user',owner=None,group_id=None):
    pid=client.get('/v1/me',headers=as_(username)).json()['principal_id']
    for namespace in ('secrets','certificates'):
        result=client.post('/v1/permissions/assignments',headers=as_('admin'),json={
            'target_id':'secret:'+scope+':'+(owner or username),'namespace':namespace,
            **({'app_group_id':group_id} if group_id else {'principal_id':pid}),
            'role':'Owner' if namespace=='certificates' else 'Admin'})
        assert result.status_code==201,result.text


def git_permissions(client,username):
    pid=client.get('/v1/me',headers=as_(username)).json()['principal_id']
    client.post('/v1/permissions/assignments',headers=as_('admin'),json={
        'target_id':'git-credentials','namespace':'git.credentials','principal_id':pid,'role':'Owner'}).raise_for_status()
