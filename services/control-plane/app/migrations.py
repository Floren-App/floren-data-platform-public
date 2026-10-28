"""Versioned data migrations layered over the existing additive schema bootstrap.

Revisions are transactional/idempotent. Existing native credentials are never inferred
into permission levels; legacy grants remain available only for migration inventory.
"""
from sqlalchemy import text, select
from .db import Base, engine, SessionLocal
from .models import Workload, Run, AuditEvent, ServiceAccount, Workspace


def upgrade():
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS fdp_migrations (revision VARCHAR(64) PRIMARY KEY)"))
    with SessionLocal() as db:
        if db.execute(text("SELECT revision FROM fdp_migrations WHERE revision='001_rbac'")).first():
            return
        from .rbac import root, workload_resource
        root(db)
        for pub in db.scalars(select(Workload)):
            # Never copy deployer memberships to a service principal.
            if not pub.execution_mode:
                pub.execution_mode = "fixed" if pub.kind in ("app", "airflow", "dbt") else "caller"
            workload_resource(db, pub)
            pub.owner_groups = []
        from .rbac import service_account_resource
        for sa in db.scalars(select(ServiceAccount)):
            service_account_resource(db, sa)
        from .rbac import principal
        for ws in db.scalars(select(Workspace)):
            if not ws.principal_id:
                ws.principal_id = principal(db, "user", ws.owner, ws.owner).id
            if not ws.volume_owner:
                ws.volume_owner = ws.owner
        for run in db.scalars(select(Run)):
            if not run.principal_id and not run.owner.startswith(("sa:", "service:")):
                run.principal_id = principal(db, "user", run.owner, run.owner).id
            if run.access_snapshot is None:
                run.access_snapshot = {}
            if not run.initiated_by:
                run.initiated_by = run.owner
            if not run.initiator_id and not run.initiated_by.startswith(("sa:", "service:")):
                run.initiator_id = principal(db, "user", run.initiated_by, run.initiated_by).id
        db.add(AuditEvent(actor="system", action="migration.rbac", target="001_rbac",
                          detail={"legacy_data_grants": "require explicit schema role mapping"}))
        db.execute(text("INSERT INTO fdp_migrations(revision) VALUES ('001_rbac')"))
        db.commit()


def upgrade_git_credentials():
    """004: a person may keep several Git credentials per host, each with a label (formerly one per host)."""
    with SessionLocal() as db:
        if db.execute(text("SELECT revision FROM fdp_migrations WHERE revision='004_git_credential_labels'")).first():
            return
        if db.bind.dialect.name == "postgresql":
            db.execute(text("ALTER TABLE git_credentials DROP CONSTRAINT IF EXISTS git_credentials_owner_host_key"))
        db.execute(text("UPDATE git_credentials SET label = host WHERE label IS NULL OR label = ''"))
        db.execute(text("INSERT INTO fdp_migrations(revision) VALUES ('004_git_credential_labels')"))
        db.commit()
