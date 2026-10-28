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
