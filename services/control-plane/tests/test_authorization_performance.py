"""Query budgets and revocation boundaries for transaction-local authorization reuse."""
import pytest
from sqlalchemy import delete, event, select, update
from app import rbac
from app.auth import User
from app.db import SessionLocal, engine
from app.models import DirectoryPrincipal, Resource, RoleAssignment
from .conftest import as_
from .rbac_helpers import grant_group


def test_resource_listing_query_cost_does_not_multiply_by_actions(client):
    client.get('/v1/me', headers=as_('admin')).raise_for_status()
    parent = client.post('/v1/rbac/folders', headers=as_('admin'), json={'name': 'Team'}).json()
    grant_group(client, parent['id'], 'Editors', 'Contributor', users=('ana',))
    def count():
        queries = []
        def record(conn, cursor, statement, *args):
            if statement.lstrip().upper().startswith('SELECT'):
                queries.append(statement)
        event.listen(engine, 'before_cursor_execute', record)
        try:
            response = client.get('/v1/rbac/resources', headers=as_('ana'))
            response.raise_for_status()
            return len(queries), response.json()
        finally:
            event.remove(engine, 'before_cursor_execute', record)
    before, _ = count()
    with SessionLocal() as db:
        db.add_all([Resource(kind='folder', name=f'Child {i}', parent_id=parent['id'], config={}) for i in range(80)])
        db.commit()
    after, resources = count()
    children = [r for r in resources if r['parent_id'] == parent['id']]
    assert len(children) == 80
    assert all('edit' in r['actions'] and 'assign' not in r['actions'] for r in children)
    assert after - before < 20, (before, after)


@pytest.mark.parametrize('bulk', [False, True])
def test_pending_or_bulk_revocation_invalidates_authorization(client, bulk):
    parent = client.post('/v1/rbac/folders', headers=as_('admin'), json={'name': 'Team'}).json()
    gid = grant_group(client, parent['id'], 'Editors', 'Contributor', users=('ana',))
    with SessionLocal() as db:
        resource = db.get(Resource, parent['id'])
        user = rbac.resolve_user(db, User('ana'))
        assert rbac.allowed(db, user, 'edit', resource)
        grant = db.scalar(select(RoleAssignment).where(RoleAssignment.app_group_id == gid))
        if bulk:
            db.execute(delete(RoleAssignment).where(RoleAssignment.id == grant.id))
        else:
            db.delete(grant)
        assert not rbac.allowed(db, user, 'edit', resource)
        db.rollback()
        assert rbac.allowed(db, user, 'edit', resource)
        db.commit()
        with SessionLocal() as other:
            other.execute(delete(RoleAssignment).where(RoleAssignment.app_group_id == gid))
            other.commit()
        # Reusing a Session after a transaction must not retain authorization.
        assert not rbac.allowed(db, user, 'edit', resource)


def test_cached_identity_does_not_hide_disabling_principal(client):
    client.get('/v1/me', headers=as_('ana')).raise_for_status()
    with SessionLocal() as db:
        user = rbac.resolve_user(db, User('ana'))
        db.execute(update(DirectoryPrincipal).where(DirectoryPrincipal.id == user.principal_id).values(active=False))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            rbac.resolve_user(db, User('ana'))
        assert error.value.status_code == 401


def test_layer_moves_invalidate_cached_access(client):
    from app import layers
    parent = client.post('/v1/rbac/folders', headers=as_('admin'), json={'name': 'Team'}).json()
    gid = grant_group(client, parent['id'], 'Editors', 'Contributor', users=('ana',))
    with SessionLocal() as db:
        resource = db.get(Resource, parent['id'])
        resource.config = {'layer_id': layers.PROD}
        user = User('ana', environment_id=layers.PROD, execution_context='workspace:example', active_group_id=gid)
        assert rbac.allowed(db, user, 'edit', resource)
        resource.config = {'layer_id': layers.NON_PROD}
        assert not rbac.allowed(db, user, 'edit', resource)


def test_unchanged_oidc_claims_do_not_rewrite_memberships(client):
    client.get('/v1/me', headers=as_('ana')).raise_for_status()
    writes = []
    def record(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) and 'directory_memberships' in statement:
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        client.get('/v1/me', headers=as_('ana')).raise_for_status()
        assert not writes
    finally:
        event.remove(engine, 'before_cursor_execute', record)
