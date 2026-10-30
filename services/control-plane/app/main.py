from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from .config import get_settings
from .db import Base, SessionLocal, engine
from .deps import get_orchestrator, get_secret_store
from .programs import json_schema
from .deploying import gitops_loop
from .reaper import reaper_loop
from .runtimes import sync_installation_runtimes
from .routes import layers, admin, apis, connect, gateway, identity, deploy, runs, workspaces, rbac, scim, delegation, sql, data_browser, catalog, measurements, compute, workspace_templates, git_credentials, packages, query_log, data_policies

logging.basicConfig(level=logging.INFO)



# Columns renamed after release, as {table: {old: new}}; existing values are kept.
RENAMED_COLUMNS = {"compute_pools": {"max_concurrent_runs": "max_concurrent"}}


def add_missing_columns() -> None:
    """Dev-time stand-in for migrations: rename, then add columns introduced after a table was created."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"]: c for c in insp.get_columns(table.name)}
            for old, new in RENAMED_COLUMNS.get(table.name, {}).items():
                if old in existing and new not in existing:
                    conn.execute(text(f'ALTER TABLE {table.name} RENAME COLUMN "{old}" TO "{new}"'))
                    existing[new] = existing.pop(old)
            for col in table.columns:
                if col.name not in existing:
                    ddl = col.type.compile(dialect=engine.dialect)
                    default = col.default.arg if col.default is not None and col.default.is_scalar else None
                    literal = col.type.literal_processor(engine.dialect) if default is not None else None
                    if literal:  # existing rows get the model's default, e.g. a new flag is false
                        ddl += f" DEFAULT {literal(default)}"
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))
                elif col.nullable and not existing[col.name]["nullable"] and engine.dialect.name == "postgresql":
                    conn.execute(text(f'ALTER TABLE {table.name} ALTER COLUMN "{col.name}" DROP NOT NULL'))


def seed() -> None:
    with SessionLocal() as db:
        # Warehouses come only from configuration (OpenTofu or an administrator), never by default.
        sync_installation_runtimes(db, get_settings())
        from .layers import migrate
        migrate(db)
        from .ownership import migrate as migrate_ownership
        migrate_ownership(db)
        from .rbac import root
        root(db)
        from .platform_metadata import setup as setup_metadata
        setup_metadata(db)
        db.commit()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.initialized = False
    # Dev convenience; production should run migrations (Alembic) instead.
    Base.metadata.create_all(engine)
    add_missing_columns()
    from .catalog_search import ensure_index
    ensure_index(engine)
    from .migrations import upgrade, upgrade_git_credentials
    upgrade()
    upgrade_git_credentials()
    seed()
    with SessionLocal() as db:  # create well-known roots up front rather than racing on first use
        from .rbac import api_root, packages_root, root
        root(db)
        api_root(db)
        packages_root(db)
        db.commit()
    tasks = []
    leadership = None
    if get_settings().orchestrator != "fake":
        s = get_settings()
        orch, secrets = get_orchestrator(), get_secret_store()
        if s.query_executor == "trino":
            from .sql_access import SQL_KINDS
            from .models import DataSource
            with SessionLocal() as db:
                for source in db.scalars(select(DataSource).where(DataSource.kind.in_(SQL_KINDS))):
                    orch.sync_data_source_egress(source.name, [])
        # Background work runs on one replica at a time (the leader); every replica serves requests.
        from .sla_monitor import monitor_loop
        from .workspace_lifecycle import readiness_loop
        from .package_imports import import_loop
        loops = [lambda: monitor_loop(SessionLocal, s.sla_evaluation_interval_seconds),
                 lambda: readiness_loop(SessionLocal, orch, s),
                 lambda: reaper_loop(SessionLocal, orch, s),
                 lambda: gitops_loop(SessionLocal, orch, secrets, s),
                 lambda: import_loop(SessionLocal)]
        from .query_log import retention_loop
        loops.append(lambda: retention_loop(SessionLocal))
        from .siem import siem_loop, sinks_safe
        if sinks_safe(s):   # audit trail and data access to the configured SIEM destinations
            loops.append(lambda: siem_loop(SessionLocal, s))
        if s.layer_namespace_prefix:  # managed layers: scale their Trino workers with load
            from .trino_scaler import scale_loop
            loops.append(lambda: scale_loop(SessionLocal))
        if hasattr(orch, 'write_layer_secret'):
            from .source_catalogs import catalog_loop
            loops.append(lambda: catalog_loop(SessionLocal, orch, secrets))
        if s.gateway_tls_endpoints:
            from .cert_monitor import gateway_loop
            loops.append(lambda: gateway_loop(SessionLocal, s.gateway_tls_endpoints, s.certificate_check_interval_seconds))
        from .leadership import Leadership
        leadership = Leadership(engine, loops)
        app.state.leadership = leadership
        tasks.append(asyncio.create_task(leadership.run()))
    app.state.initialized = True
    yield
    app.state.initialized = False
    for task in tasks:
        task.cancel()
    if leadership is not None:
        leadership.shutdown()
    from . import sql_access
    await sql_access.close_engine_client()
    for _, native_engine in sql_access._native_engines.values():
        native_engine.dispose()
    sql_access._native_engines.clear()


app = FastAPI(title="Floren Data Platform control plane", version="0.1.0", lifespan=lifespan)
from .secrets import SecretBackendError


@app.exception_handler(SecretBackendError)
async def secret_backend_unavailable(request: Request, exc: SecretBackendError):
    return JSONResponse(status_code=503, content={'detail':str(exc)})

app.include_router(measurements.router)
@app.middleware("http")
async def count_requests(request: Request, call_next):
    """Request counts and durations per route template (never the concrete path: no IDs in metric labels)."""
    from . import metrics
    timer = metrics.Timer()
    response = await call_next(request)
    route = request.scope.get("route")
    metrics.observe(request.method, getattr(route, "path", "unmatched"), response.status_code, timer.seconds())
    return response


app.add_middleware(
    CORSMiddleware,
    # Desktop app origins: Tauri (macOS/Linux, Windows) and the Vite dev server.
    allow_origins=["tauri://localhost", "http://tauri.localhost", "https://localhost:1420", "http://localhost:1420"],
    allow_methods=["*"],
    allow_headers=["*"],
)
for module in (workspaces, workspace_templates, git_credentials, packages, query_log, runs, compute, admin, connect, deploy, identity, gateway, rbac, scim, delegation, sql, data_browser, catalog, apis, layers, data_policies):
    app.include_router(module.router)
app.include_router(gateway.docs_router)


@app.exception_handler(OperationalError)
async def database_unavailable(request: Request, exc: OperationalError):
    # e.g. disk full, connection lost: tell the user why instead of a bare 500.
    reason = str(exc.orig).splitlines()[0] if exc.orig else "database error"
    logging.getLogger("fdp").error("database error on %s %s: %s", request.method, request.url.path, reason)
    return JSONResponse(status_code=503, content={"detail": f"Database unavailable: {reason}"})


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get('/readyz', include_in_schema=False)
def readyz():
    """Route traffic only after initialization and while metadata is reachable.

    Dependency failure removes this replica from endpoints, without turning a
    database outage into a liveness restart loop. Never expose connection errors.
    """
    if not getattr(app.state, 'initialized', False):
        return JSONResponse(status_code=503, content={'ok': False, 'reason': 'Initializing'})
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={'ok': False, 'reason': 'MetadataUnavailable'})
    return {'ok': True}


@app.get("/v1/schema/fdp.json", tags=["meta"])
def fdp_schema():
    """JSON Schema for a workspace's fdp.json (also in schemas/ for editors)."""
    return json_schema()


@app.get('/v1/schema/layer-infrastructure.json', tags=['meta'])
def layer_infrastructure_schema():
    from .layer_infrastructure import InfrastructureSpec
    return InfrastructureSpec.model_json_schema()


@app.get("/v1/client-config", tags=["meta"])
def client_config():
    """Unauthenticated: lets the desktop app discover how to sign in from just the server URL."""
    s = get_settings()
    return {"oidc_issuer": s.oidc_issuer, "oidc_client_id": s.oidc_client_id,
            "oidc_scopes": s.oidc_scopes, "public_url": s.public_url, "query_executor": s.query_executor}


@app.exception_handler(HTTPException)
async def api_error(request: Request, exc):
    if request.url.path.startswith("/scim/"):
        detail = exc.detail if isinstance(exc.detail, dict) and "schemas" in exc.detail else {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:Error"],
            "status": str(exc.status_code), "detail": str(exc.detail)}
        return JSONResponse(detail, status_code=exc.status_code, media_type="application/scim+json")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
