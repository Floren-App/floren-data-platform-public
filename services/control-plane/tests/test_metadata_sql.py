"""Management SQL uses the same authorization and invariants as UI operations."""
import json
from urllib.parse import quote_plus

import httpx
import pytest
from sqlalchemy import select

from app import platform_metadata as metadata, rbac, sql_access
from app.auth import User
from app.config import get_settings
from app.db import SessionLocal, engine
from app.models import AppGroup, AuditEvent, DataSource, IdentityBinding, Resource, RoleAssignment, UserPreference
from app.ownership import GLOBAL_ADMIN
from .conftest import as_
from .test_shared_sql import sql_mode


@pytest.fixture
def metadata_mode(sql_mode, monkeypatch):
    client, execution = sql_mode
    monkeypatch.setattr(get_settings(), "metadata_sql_enabled", True)
    with SessionLocal() as db:
        ds = DataSource(id=metadata.SOURCE_ID, name=metadata.SOURCE_NAME, kind="postgres", config={"catalog": "fdp_metadata", "system_metadata": True})
        db.add(ds); db.flush()
        root = rbac.source_resource(db, ds)
        db.add(Resource(id=metadata.SCHEMA_ID, kind="schema", name="public", parent_id=root.id, object_id=ds.id, config={"state": "ready", "system_metadata": True}))
        db.commit()
    return client, execution


def command(client, statement, user="admin", **headers):
    return client.post("/v1/statement", headers={**as_(user), "X-Trino-Catalog": "fdp_metadata", **headers}, content=statement)


def test_metadata_discovery_and_policy_require_active_global_admin(metadata_mode, monkeypatch):
    client, execution = metadata_mode
    def visible(headers):
        return [r["id"] for r in client.get("/v1/rbac/resources", headers=headers).json()]
    assert metadata.SCHEMA_ID in visible(as_("admin"))
    assert metadata.SCHEMA_ID not in visible(as_("ana"))
    with SessionLocal() as db:
        assert "fdp_metadata" in sql_access.scopes(db, sql_access.actor(db, "Bearer admin"))
        assert "fdp_metadata" not in sql_access.scopes(db, sql_access.actor(db, execution["Authorization"]))
    assert command(client, "UPDATE app_groups SET description='x' WHERE id='000000000001'", user="ana").status_code == 403
    group = client.post('/v1/rbac/groups', headers=as_('admin'), json={"name": "Admin secondary", "owners": [client.get('/v1/me', headers=as_('admin')).json()['principal_id']]}).json()
    client.post('/v1/rbac/groups/'+group['id']+'/bindings', headers=as_('admin'), json={"principal_id": client.get('/v1/me', headers=as_('admin')).json()['principal_id']}).raise_for_status()
    client.put('/v1/me/active-group', headers=as_('admin'), json={"group_id": group['id']}).raise_for_status()
    assert metadata.SCHEMA_ID not in visible(as_("admin"))
    with SessionLocal() as db:
        assert "fdp_metadata" not in sql_access.scopes(db, sql_access.actor(db, "Bearer admin"))
    assert command(client, "INSERT INTO app_groups (name) VALUES ('Denied')").status_code == 403


def test_group_lifecycle_and_read_only_tables(metadata_mode):
    client, _ = metadata_mode
    result = command(client, "INSERT INTO app_groups (name, description) VALUES ('SQL readers', 'Created using SQL')")
    assert result.status_code == 200, result.text
    assert result.json()["updateCount"] == 1
    with SessionLocal() as db:
        group = db.scalar(select(AppGroup).where(AppGroup.name == 'SQL readers'))
        gid = group.id
        assert group.owners
    result = command(client, f"UPDATE app_groups SET description='Updated' WHERE id='{gid}'")
    assert result.status_code == 200, result.text
    result = client.post('/v1/sql/query', headers=as_('admin'), json={"source": metadata.SOURCE_NAME, "sql": "UPDATE app_groups SET description=? WHERE id=?", "parameters": ["Parameters work", gid]})
    assert result.status_code == 200, result.text
    assert command(client, f"DELETE FROM app_groups WHERE id='{gid}'").status_code == 200
    for sql in ["DELETE FROM audit_events WHERE id='x'", "UPDATE workspaces SET status='running' WHERE id='x'", "UPDATE app_groups SET description='everyone'", "DELETE FROM app_groups WHERE id='000000000001'", "UPDATE app_groups SET name='Renamed' WHERE id='000000000001'"]:
        assert command(client, sql).status_code in (409, 422)
    with SessionLocal() as db:
        assert db.get(AppGroup, gid) is None
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'sql.metadata_change'))


def test_grants_bindings_and_prepared_mutations(metadata_mode):
    client, _ = metadata_mode
    assert command(client, "INSERT INTO app_groups (name) VALUES ('SQL grants')").status_code == 200
    with SessionLocal() as db:
        gid = db.scalar(select(AppGroup.id).where(AppGroup.name == 'SQL grants'))
        schema = db.scalar(select(Resource).where(Resource.kind == 'schema', Resource.name == 'reporting'))
        rid = schema.id
        ana = rbac.resolve_user(db, User('ana')).principal_id
    grant = command(client, f"GRANT Reader ON RESOURCE '{rid}' TO GROUP 'SQL grants'")
    assert grant.status_code == 200, grant.text
    with SessionLocal() as db:
        assignment = db.scalar(select(RoleAssignment).where(RoleAssignment.app_group_id == gid))
        assert assignment.role == 'Reader'
        aid = assignment.id
    prepared = "UPDATE fdp_metadata.public.role_assignments SET role=? WHERE id=?"
    result = command(client, f"EXECUTE change_role USING 'Contributor', '{aid}'", **{"X-Trino-Prepared-Statement": "change_role=" + quote_plus(prepared)})
    assert result.status_code == 200, result.text
    result = command(client, f"INSERT INTO identity_bindings (app_group_id, principal_id) VALUES ('{gid}', '{ana}')")
    assert result.status_code == 200, result.text
    result = command(client, f"DELETE FROM identity_bindings WHERE app_group_id='{gid}' AND principal_id='{ana}'")
    assert result.status_code == 200, result.text
    assert command(client, f"REVOKE Reader ON RESOURCE '{rid}' FROM GROUP 'SQL grants'").status_code == 422
    assert command(client, f"REVOKE Contributor ON RESOURCE '{rid}' FROM GROUP 'SQL grants'").status_code == 200
    assert command(client, f"GRANT Reader ON RESOURCE '{metadata.SCHEMA_ID}' TO GROUP 'SQL grants'").status_code == 403


def test_management_sql_rejects_extra_statements_and_expressions(metadata_mode):
    client, _ = metadata_mode
    for sql in [
        "INSERT INTO app_groups (name) VALUES ('Never created'); SELECT 1",
        "INSERT INTO app_groups (name) VALUES ('Never created'), ('Another row')",
        "INSERT INTO app_groups (name) SELECT 'Never created'",
        "INSERT INTO app_groups (name) VALUES (current_user)",
        "INSERT INTO app_groups (name) VALUES ('Never created') RETURNING id",
        "INSERT INTO app_groups (name) VALUES ('Never created') ON CONFLICT (name) DO NOTHING",
        "UPDATE app_groups SET description='x' FROM app_groups AS other WHERE id='000000000001'",
    ]:
        response = command(client, sql)
        assert response.status_code == 422, (sql, response.text)
    with SessionLocal() as db:
        assert not db.scalar(select(AppGroup).where(AppGroup.name == 'Never created'))


def test_metadata_engine_policy_is_read_only_and_names_bounded(metadata_mode):
    client, _ = metadata_mode
    with SessionLocal() as db:
        scope = sql_access.scopes(db, sql_access.actor(db, 'Bearer admin'))
    from app.models import SqlQuery
    query = SqlQuery(scope=scope)
    resource = {"table": {"catalogName": "fdp_metadata", "schemaName": "public", "tableName": "app_groups"}}
    assert sql_access.policy_allow(query, {"operation": "SelectFromColumns", "resource": resource})
    for operation in ("UpdateTableColumns", "DeleteFromTable", "InsertIntoTable", "DropTable", "CreateView"):
        assert not sql_access.policy_allow(query, {"operation": operation, "resource": resource})
    resource['table']['tableName'] = 'unrelated_table'
    assert not sql_access.policy_allow(query, {"operation": "SelectFromColumns", "resource": resource})


def test_sql_direct_service_grants_cannot_cross_layers(metadata_mode):
    from app.models import DirectoryPrincipal
    from app.layers import PROD
    client, _ = metadata_mode
    with SessionLocal() as db:
        principal = DirectoryPrincipal(kind='service', provider='local', external_id='prod-service', name='Prod service', active=True)
        db.add(principal); db.flush()
        db.add(Resource(kind='service', name='Prod service', object_id='prod-service', config={'layer_id': PROD}))
        schema = db.scalar(select(Resource).where(Resource.kind == 'schema', Resource.name == 'reporting'))
        rid, pid = schema.id, principal.id
        db.commit()
    response = command(client, f"INSERT INTO service_role_assignments (resource_id, principal_id, role) VALUES ('{rid}', '{pid}', 'Contributor')")
    assert response.status_code == 422 and 'same layer' in response.text
    response = command(client, f"GRANT Contributor ON RESOURCE '{rid}' TO SERVICE '{pid}'")
    assert response.status_code == 422 and 'same layer' in response.text


def test_sql_cannot_unbind_protected_admin_and_catalog_changes_validate(metadata_mode):
    from app.models import DirectoryPrincipal, CatalogAsset
    from app.catalog import upsert
    client, _ = metadata_mode
    with SessionLocal() as db:
        admin_role = db.scalar(select(DirectoryPrincipal).where(DirectoryPrincipal.kind == 'role', DirectoryPrincipal.name == 'fdp-admin'))
        pid = admin_role.id
        resource = db.scalar(select(Resource).where(Resource.kind == 'schema', Resource.name == 'reporting'))
        asset = upsert(db, resource, key='object:orders', kind='table', name='orders')
        aid = asset.id
        db.commit()
    assert command(client, f"DELETE FROM identity_bindings WHERE app_group_id='{GLOBAL_ADMIN}' AND principal_id='{pid}'").status_code == 409
    assert command(client, f"UPDATE catalog_assets SET governance='{{\"status\":\"certified\"}}' WHERE id='{aid}'").status_code == 422
    response = command(client, f"UPDATE catalog_assets SET sla='{{\"freshness_minutes\":60}}' WHERE id='{aid}'")
    assert response.status_code == 200, response.text
    response = command(client, f"UPDATE catalog_assets SET sla='{{\"freshness_minutes\":0}}' WHERE id='{aid}'")
    assert response.status_code == 422, response.text


@pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='PostgreSQL reader bootstrap')
def test_metadata_bootstrap_registers_every_table_and_uses_a_read_only_account(metadata_mode, monkeypatch):
    from sqlalchemy import create_engine, text, func
    from sqlalchemy.exc import DBAPIError
    from app.models import CatalogAsset
    client, _ = metadata_mode
    settings = get_settings()
    monkeypatch.setattr(settings, 'metadata_reader_user', 'fdp_metadata_test_reader')
    monkeypatch.setattr(settings, 'metadata_reader_password', 'metadata-test-only-password')
    with SessionLocal() as db:
        metadata.setup(db)
        db.commit()
        assert db.scalar(select(func.count()).select_from(CatalogAsset).where(CatalogAsset.resource_id == metadata.SCHEMA_ID)) == len(metadata.table_names())
        assert not db.get(DataSource, metadata.SOURCE_ID).config.get('password')
    reader = create_engine(engine.url.set(username=settings.metadata_reader_user, password=settings.metadata_reader_password))
    try:
        with reader.connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM app_groups')).scalar() >= 1
            conn.commit()
            conn.execute(text('SET default_transaction_read_only=off'))
            conn.commit()
            with pytest.raises(DBAPIError, match='permission denied'):
                conn.execute(text("UPDATE app_groups SET description='Cannot mutate directly'"))
    finally:
        reader.dispose()
