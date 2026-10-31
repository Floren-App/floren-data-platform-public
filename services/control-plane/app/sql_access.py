"""Shared SQL admission and Trino's OPA wire protocol, backed by the existing RBAC evaluator.

The OPA plugin calls this policy endpoint directly. No second role store or OPA daemon
is needed. Unknown operations and unsigned contexts fail closed.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import ssl
from urllib.parse import urlsplit

import httpx
import jwt
from sqlglot.dialects import Dialect
from fastapi import HTTPException
from sqlalchemy import select

from . import rbac
from .auth import User, Execution, get_verifier, mint_token, read_token, validate_execution
from .config import get_settings
from .models import (DataSource, DirectoryPrincipal, Workload, Resource,
                     Run, SqlQuery, utcnow)

SQL_KINDS = {"postgres", "iceberg"}
LEVELS = {"Reader": 1, "Contributor": 2, "Admin": 3, "Owner": 4}


class GatewayTokenizer(Dialect.get_or_raise("trino").tokenizer_class):
    COMMANDS = set()


def catalog_name(source):
    import re
    name = source.config.get("catalog", source.name.replace("-", "_"))
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", name):
        raise HTTPException(409, "source needs a valid lowercase Trino catalog name")
    if name == "system":
        raise HTTPException(409, "the system catalog cannot be registered as a source")
    return name


def catalog_sources(db):
    sources = {}
    for source in db.scalars(select(DataSource).where(DataSource.kind.in_(SQL_KINDS))):
        name = catalog_name(source)
        if name in sources:
            raise HTTPException(409, "two data sources map to the same Trino catalog")
        sources[name] = source
    return sources


@dataclass
class SqlActor:
    user: User
    binding: str
    execution: Execution = None
    viewer: str = ""
    workload_id: str = ""
    scope_limit: dict = None  # Internal metric evaluation only; never accepted from HTTP headers.


def actor(db, authorization, viewer=""):
    """Authenticate independently of every X-Trino-* identity supplied by a client."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.split(" ", 1)[1]
    if token.startswith("dcs_"):
        from .deps import service_token_user
        user = service_token_user(token)
        # Distinguish individual service tokens for continuation ownership.
        import hashlib
        return SqlActor(user, "service:" + hashlib.sha256(token.encode()).hexdigest())
    try:
        claims = read_token(token, "execution")
    except HTTPException:
        claims = None
        # dbt's standard adapter keeps a static profile token. As with execution
        # renewal, an expired signed job token remains usable for that active job's
        # SQL snapshot only; editor/workload tokens get no such exception.
        try:
            from .auth import decode_session
            expired = decode_session(token, verify_exp=False)
            if expired.get("typ") == "execution" and expired.get("run"):
                claims = expired
        except jwt.PyJWTError:
            pass
    if claims:
        from .environments import execution_user, bind
        validate_execution(claims)
        execution = Execution(username=claims["sub"], groups=[], workspace_id=claims.get("ws", ""),
            workload_id=claims.get("pub", ""), run_id=claims.get("run", ""),
            principal_id=claims.get("pid", ""), commit_sha=claims.get("sha", ""))
        binding = "run:" + execution.run_id if execution.run_id else (
            "workspace:" + execution.workspace_id if execution.workspace_id else
            "workload:" + execution.workload_id + ":" + execution.commit_sha)
        if viewer:
            pub = db.get(Workload, execution.workload_id) if execution.workload_id else None
            delegated = read_token(viewer, "viewer")
            if (not pub or pub.status != "live" or pub.execution_mode != "caller" or
                    delegated.get("pub") != pub.id or execution.username != pub.identity):
                raise HTTPException(403, "viewer context does not belong to this caller-mode app")
            user = rbac.resolve_user(db, bind(db, User(delegated["sub"], principal_id=delegated.get("pid", "")), rbac.workload_resource(db, pub)))
            rbac.require(db, user, "execute", rbac.workload_resource(db, pub))
            return SqlActor(user, binding + ":viewer:" + user.principal_id, execution, viewer, pub.id)
        if execution.workload_id:
            pub = db.get(Workload, execution.workload_id)
            if pub and pub.kind == "app" and pub.execution_mode == "caller":
                raise HTTPException(403, "caller-mode apps require a viewer context on every SQL request")
        # A running job retains admission privileges even after its principal is disabled.
        if execution.run_id:
            run = db.get(Run, execution.run_id)
            return SqlActor(User(execution.username, principal_id=run.principal_id or execution.principal_id,
                environment_id=run.environment_id or "", execution_context=binding,
                active_group_id=run.primary_group_id or ""), binding, execution)
        user = rbac.resolve_user(db, execution_user(db, claims))
        return SqlActor(user, binding, execution, workload_id=execution.workload_id)
    if viewer:
        raise HTTPException(403, "viewer delegation requires its app execution")
    user = rbac.resolve_user(db, get_verifier().verify(token), refresh=True)
    return SqlActor(user, "user:" + user.principal_id)


def scopes(db, who):
    """Evaluate the data resource tree once per admission, rather than once per schema."""
    job = db.get(Run, who.execution.run_id) if who.execution and who.execution.run_id else None
    if who.execution and who.execution.run_id and not job:
        raise HTTPException(401, "run is no longer active")
    if job and (job.status not in ("pending", "running") or job.owner != who.user.username):
        raise HTTPException(401, "run is no longer active")
    grants = {}
    if not job:
        if not db.scalar(select(DirectoryPrincipal.active).where(DirectoryPrincipal.id == who.user.principal_id)):
            raise HTTPException(401, "identity is inactive")
        for grant in rbac.role_grants(db, who.user):
            if grant.role in LEVELS and LEVELS[grant.role] > LEVELS.get(grants.get(grant.resource_id), 0):
                grants[grant.resource_id] = grant.role
    schemas = list(db.scalars(select(Resource).where(Resource.kind == "schema")))
    from .ownership import creation_data_snapshot
    primary = (job.primary_create_snapshot if job.primary_create_snapshot is not None else job.access_snapshot) if job else creation_data_snapshot(db,who.user)
    result = {}
    for catalog, source in catalog_sources(db).items():
        from . import platform_metadata as metadata
        if source.id == metadata.SOURCE_ID:
            if get_settings().metadata_sql_enabled and metadata.authorized(who.user) and not who.execution:
                result[catalog] = {"source_id": source.id, "kind": "platform_metadata", "system_metadata": True,
                                   "schemas": {get_settings().metadata_schema: "Reader"}, "create_schemas": [],
                                   "tables": metadata.table_names()}
            continue
        scope = {}
        for schema in schemas:
            if schema.object_id != source.id or schema.config.get("state") != "ready":
                continue
            if who.user.is_admin and not who.user.execution_context:
                role = "Owner"
            elif job:
                role = (job.access_snapshot or {}).get(source.id, {}).get(schema.name)
            else:
                role = max((grants.get(schema.id), grants.get(schema.parent_id)), key=lambda x: LEVELS.get(x, 0))
            if role in LEVELS:
                scope[schema.name] = role
        if scope:
            result[catalog] = {"source_id": source.id, "kind": source.kind, "schemas": scope,
                               "append_only_schemas": [s.name for s in schemas if s.object_id == source.id and
                                                       s.name in scope and s.config.get('append_only')],
                               "create_schemas":[name for name in scope if LEVELS.get(primary.get(source.id,{}).get(name),0) >= 3]}
    if who.scope_limit is not None:
        result = {catalog: {**scope, 'schemas': {name: 'Reader' for name in scope['schemas'] if name in who.scope_limit.get(catalog, [])}, 'create_schemas': []}
                  for catalog, scope in result.items() if catalog in who.scope_limit and not scope.get('system_metadata')}
        result = {catalog: scope for catalog, scope in result.items() if scope['schemas']}
    return result


def check_native_relations(query):
    """Native PostgreSQL views/foreign tables can hide dependencies from Trino's analyzer.

    Read this inventory at admission, using an engine-only credential. Native views are
    unavailable on this path; use Trino invoker views or materialized data instead.
    Infrastructure administrators must not change native object kinds during queries.
    """
    from .deps import get_secret_store
    from .secrets import datasource_path
    from .db import SessionLocal
    from sqlalchemy.exc import SQLAlchemyError
    with SessionLocal() as db:
        for catalog, scope in query.scope.items():
            if scope["kind"] != "postgres":
                continue
            source = db.get(DataSource, scope["source_id"])
            cfg = source.config
            cred = get_secret_store().read(datasource_path(source.name))
            if not cred.get("username") or not cred.get("password"):
                raise HTTPException(409, "engine-only PostgreSQL connection credential is not configured")
            try:
                with native_metadata_engine(source, cred).connect() as conn:
                    records = conn.exec_driver_sql("SELECT n.nspname,c.relname FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname = ANY(%s) "
                        "AND c.relkind IN ('v','f')", (list(scope["schemas"]),)).fetchall()
            except SQLAlchemyError:
                raise HTTPException(503, "cannot verify PostgreSQL object dependencies") from None
            scope["blocked_relations"] = [list(row) for row in records]


_native_engines = {}
from threading import Lock
_native_lock = Lock()


def native_metadata_engine(source, credential):
    """Reuse infrastructure metadata connections; never create a client-visible SQL path."""
    import hashlib
    import json
    from sqlalchemy import create_engine, URL
    fingerprint = hashlib.sha256(json.dumps([source.config, credential], sort_keys=True).encode()).hexdigest()
    with _native_lock:
        cached = _native_engines.get(source.id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        if cached:
            cached[1].dispose()
        if len(_native_engines) >= 32:
            _native_engines.pop(next(iter(_native_engines)))[1].dispose()
        cfg = source.config
        engine = create_engine(URL.create("postgresql+psycopg", host=cfg.get("host"),
            port=cfg.get("port", 5432), database=cfg.get("database"), username=credential["username"],
            password=credential["password"]), pool_size=4, max_overflow=4, pool_timeout=5,
            pool_pre_ping=True, isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 5,
            **({"sslmode": cfg["sslmode"]} if cfg.get("sslmode") else {})})
        _native_engines[source.id] = (fingerprint, engine)
        return engine


def engine_layer(db, who, requested=""):
    """Server-derived layer engine. Executions use their bound layer; interactive users may
    name one. Returns "" for layers still served by the shared legacy engine."""
    from . import layers
    bound = who.user.environment_id if (who.execution or who.user.execution_context) else ""
    if not (bound or requested):
        return ""
    layer = layers.require_layer(db, bound or requested)
    if not (layer.config.get("infrastructure") or {}).get("enabled"):
        return ""  # Shared legacy engine; behavior unchanged.
    if bound and requested and layers.canonical(db, requested) != layer.id:
        raise HTTPException(403, "executions query only their own layer")
    from .layer_infrastructure import engine_ready
    if not engine_ready(db, layer):
        raise HTTPException(409, {"code": "LayerNotReady", "message": "this layer's SQL engine is still provisioning"})
    if not get_settings().layer_namespace_prefix:
        raise HTTPException(503, "layer SQL engine routing is not configured")
    return layer.id


def layer_scope(db, scope, layer):
    """A layer engine only has that layer's catalogs; platform metadata stays on its own path."""
    from .layers import canonical
    result = {}
    for catalog, item in scope.items():
        source = db.get(DataSource, item["source_id"])
        if not item.get("system_metadata") and source and canonical(db, (source.config or {}).get("layer_id")) == layer:
            result[catalog] = item
    return result


def admit(db, who, catalog="", layer=""):
    engine_scheme = 'http'
    if layer:
        resource = db.scalar(select(Resource).where(Resource.id == layer).with_for_update()
                             .execution_options(populate_existing=True))
        from .layer_infrastructure import engine_ready
        if not resource or not engine_ready(db, resource):
            raise HTTPException(409, {'code': 'LayerNotReady', 'message': "this layer's SQL engine is still provisioning"})
        if resource.config.get('infrastructure', {}).get('trino', {}).get('tls', {}).get('mode', 'disabled') != 'disabled':
            engine_scheme = 'https'
    scope = scopes(db, who)
    if layer:
        scope = layer_scope(db, scope, layer)
    if catalog and catalog not in scope:
        raise HTTPException(404, "catalog not available")
    if not scope:
        raise HTTPException(403, "no SQL schema grants")
    # Column masks and row filters that apply to this caller, fixed for the query like its grants.
    from .data_policies import attach
    scope = attach(db, who.user, scope)
    from .product_options import attach as attach_product_options
    scope = attach_product_options(db, who.user, scope)
    run = db.get(Run, who.execution.run_id) if who.execution and who.execution.run_id else None
    query = SqlQuery(principal_id=who.user.principal_id, actor=who.user.username,
                     initiator_id=(run.initiator_id or "") if run else who.user.principal_id,
                     binding=who.binding, run_id=run.id if run else "",
                     workload_id=who.workload_id or (run.workload_id or "" if run else ""),
                     workspace_id=(who.execution.workspace_id if who.execution else "") or ((run.workspace_id or "") if run else ""),
                     scope=scope, engine_layer=layer,
                     engine_scheme=engine_scheme,
                     expires_at=utcnow() + timedelta(seconds=get_settings().sql_query_ttl_seconds))
    check_native_relations(query)
    db.add(query)
    db.flush()
    # Persist before Trino makes policy callbacks on a separate DB connection.
    db.commit()
    return query


def context_token(query):
    return mint_token("trino-policy", query.principal_id, get_settings().sql_query_ttl_seconds,
                      q=query.id)


def engine_headers(query):
    settings = get_settings()
    if query.engine_layer:
        token = layer_engine_token(query.principal_id, query.engine_layer)
    else:
        if not settings.trino_signing_key:
            raise HTTPException(503, "Trino infrastructure authentication is not configured")
        token = jwt.encode({"sub": query.principal_id, "iss": "fdp-sql", "aud": "trino",
                            "exp": int(time.time()) + settings.sql_query_ttl_seconds},
                           settings.trino_signing_key, algorithm="HS256")
    return {"Authorization": "Bearer " + token, "X-Trino-User": query.principal_id,
            "X-Trino-Extra-Credential": "dc_context=" + context_token(query),
            "X-Trino-Source": "fdp", "X-Trino-Trace-Token": query.id,
            "X-Trino-Client-Tags": "fdp"}


def layer_audience(layer_id):
    return "trino-layer-" + layer_id


def _layer_key():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    pem = get_settings().trino_jwt_private_key
    if not pem:
        raise HTTPException(503, "Layer Trino authentication is not configured")
    return load_pem_private_key(pem.encode(), password=None)


def _key_id(public):
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    import hashlib
    return hashlib.sha256(public.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest()[:16]


def layer_engine_token(principal_id, layer_id):
    """Accepted only by the Trino whose required audience names this layer."""
    key = _layer_key()
    return jwt.encode({"sub": principal_id, "iss": "fdp-sql", "aud": layer_audience(layer_id),
                       "exp": int(time.time()) + get_settings().sql_query_ttl_seconds},
                      key, algorithm="ES256", headers={"kid": _key_id(key.public_key())})


def layer_jwks():
    """The signing key plus former keys during a rotation, so in-flight query tokens keep verifying."""
    import re
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    previous = re.findall(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                          get_settings().trino_jwt_previous_keys, re.S)
    keys = [_layer_key().public_key(), *(load_pem_private_key(p.encode(), password=None).public_key() for p in previous)]
    out = []
    for public in keys:
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(public))
        out.append({**jwk, "kid": _key_id(public), "alg": "ES256", "use": "sig"})
    return {"keys": out}


@dataclass
class _EngineTransport:
    client: httpx.AsyncClient
    fingerprint: str
    users: int = 0


_engine_transport = None


async def close_engine_client():
    global _engine_transport
    previous, _engine_transport = _engine_transport, None
    if previous and previous.users == 0:
        await previous.client.aclose()


@asynccontextmanager
async def engine_client():
    """Reload changed CA bundles; drain old requests without disabling verification."""
    global _engine_transport
    try:
        bundle_path = os.environ.get('SSL_CERT_FILE', '')
        bundle = Path(bundle_path).read_bytes() if bundle_path else None
        fingerprint = hashlib.sha256(bundle).hexdigest() if bundle is not None else 'system'
        previous = _engine_transport
        if previous is None or previous.client.is_closed or previous.fingerprint != fingerprint:
            context = ssl.create_default_context(cadata=bundle.decode('ascii')) if bundle is not None else ssl.create_default_context()
            _engine_transport = _EngineTransport(httpx.AsyncClient(verify=context,
                timeout=httpx.Timeout(45, connect=10), follow_redirects=False,
                limits=httpx.Limits(max_connections=100, max_keepalive_connections=30)), fingerprint)
        state = _engine_transport
        state.users += 1
    except (OSError, ValueError, UnicodeError, ssl.SSLError):
        raise HTTPException(503, 'SQL engine TLS trust bundle is unavailable or invalid') from None
    try:
        if previous and previous is not state and previous.users == 0:
            await previous.client.aclose()
        yield state.client
    finally:
        state.users -= 1
        if state is not _engine_transport and state.users == 0:
            await state.client.aclose()


def engine_url(query):
    if not query.engine_layer:
        return get_settings().trino_url.rstrip("/")
    from .layer_infrastructure import namespace_for
    scheme = query.engine_scheme or 'http'  # persisted queries from before HTTPS support
    if scheme not in ('http', 'https'):
        raise HTTPException(503, 'SQL engine transport configuration is invalid')
    return "%s://fdp-trino.%s.svc:%s" % (scheme,
        namespace_for(query.engine_layer, get_settings().layer_namespace_prefix), 8443 if scheme == 'https' else 8080)


async def engine_request(method, path, *, headers, query, content=None):
    try:
        async with engine_client() as client:
            return await client.request(method, engine_url(query) + path, headers=headers, content=content)
    except httpx.HTTPError:
        raise HTTPException(503, "SQL engine is unavailable") from None


def engine_path(uri):
    """Only coordinator statement continuations can be proxied. Never follow arbitrary URLs."""
    parsed = urlsplit(uri)
    # Trino advertises a coordinator IP that can differ from its configured DNS name.
    # Ignore the advertised origin entirely: every outgoing request uses the query's engine URL.
    import re
    if (parsed.scheme not in ("http", "https") or parsed.fragment or
            not re.fullmatch(r"/v1/statement/[a-zA-Z0-9_./-]+", parsed.path) or ".." in parsed.path):
        raise HTTPException(502, "invalid SQL engine continuation")
    return parsed.path + ("?" + parsed.query if parsed.query else "")


def policy_query(db, payload, layer=""):
    identity = payload.get("context", {}).get("identity", {})
    token = identity.get("extraCredentials", {}).get("dc_context", "")
    claims = read_token(token, "trino-policy")
    query = db.get(SqlQuery, claims.get("q"))
    if (not query or claims.get("sub") != query.principal_id or
            identity.get("user") != query.principal_id or
            query.state in {"FAILED", "CANCELED"} or
            query.expires_at.replace(tzinfo=utcnow().tzinfo) <= utcnow()):
        raise HTTPException(403, "invalid SQL policy context")
    if (query.engine_layer or "") != layer:
        # Each layer engine calls its own policy path; a context never crosses engines.
        raise HTTPException(403, "SQL policy context belongs to a different engine")
    engine_query_id = payload.get("context", {}).get("queryId")
    if query.engine_query_id and engine_query_id and engine_query_id != query.engine_query_id:
        raise HTTPException(403, "SQL policy context belongs to a different engine query")
    if query.run_id:
        run = db.get(Run, query.run_id)
        if not run or run.status not in ("pending", "running"):
            raise HTTPException(403, "job has ended")
    return query


READ = {"ShowCreateSchema", "ShowCreateTable", "ShowTables", "FilterSchemas", "FilterTables",
        "ShowColumns", "FilterColumns", "SelectFromColumns", "ShowFunctions"}
WRITE = {"InsertIntoTable", "DeleteFromTable", "UpdateTableColumns"}
DDL = {"CreateTable", "DropTable", "RenameTable", "SetTableProperties", "SetTableComment",
       "SetColumnComment", "AddColumn", "AlterColumn", "DropColumn", "RenameColumn", "TruncateTable",
       "CreateView", "RenameView", "DropView", "SetViewComment", "CreateSchema", "DropSchema", "RenameSchema",
       "CreateMaterializedView", "RefreshMaterializedView", "SetMaterializedViewProperties",
       "SetMaterializedViewComment", "DropMaterializedView", "RenameMaterializedView"}
# Builtins are trusted engine code. Catalog-specific query/raw_query functions are not.
SAFE_SYSTEM_PROPERTIES = {"query_max_run_time", "query_max_execution_time", "query_max_memory",
                          "join_distribution_type", "join_reordering_strategy"}


def validate_statement(text, prepared="", depth=0, on_create=None, on_stats=None):
    """Require invoker security on views, even through client-side SQL wrappers.

    Engine access control handles table privileges. The engine's CreateView callback
    lacks the security mode, so this additional structured check is necessary.
    """
    import logging
    import sqlglot
    from sqlglot import exp
    from urllib.parse import unquote_plus
    # Unsupported-command warnings include SQL text; never write user SQL into logs.
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    if depth > 8:
        raise HTTPException(422, "SQL wrapper nesting limit exceeded")
    try:
        nodes = sqlglot.parse(text, read="trino")
        # sqlglot normally collapses unsupported commands' entire tails into one
        # synthetic string token. Disable that shortcut to inspect wrapper syntax.
        tokens = GatewayTokenizer().tokenize(text)
        if len(nodes) != 1 or nodes[0] is None:
            raise ValueError()
        if on_stats and len(tokens) > 1 and tokens[0].text.upper() == "SHOW" and tokens[1].text.upper() == "STATS":
            on_stats()
        for create in nodes[0].find_all(exp.Create):
            if on_create:
                on_create(create)
            if create.args.get("kind") == "VIEW":
                security = list(create.find_all(exp.SecurityProperty))
                if not security or any(p.this != "INVOKER" for p in security):
                    raise HTTPException(422, "views must explicitly use SECURITY INVOKER")
        if tokens[0].text.upper() == "PREPARE":
            if len(tokens) < 4 or tokens[2].text.upper() != "FROM":
                raise ValueError()
            validate_statement(text[tokens[3].start:], depth=depth+1, on_create=on_create, on_stats=on_stats)
        if tokens[0].text.upper() == "EXPLAIN":
            i = 1
            while i < len(tokens) and tokens[i].text.upper() in {"ANALYZE", "VERBOSE"}:
                i += 1
            if i < len(tokens) and tokens[i].text == "(":
                nesting = 1
                i += 1
                while i < len(tokens) and nesting:
                    nesting += (tokens[i].text == "(") - (tokens[i].text == ")")
                    i += 1
            if i >= len(tokens):
                raise ValueError()
            validate_statement(text[tokens[i].start:], prepared, depth=depth+1, on_create=on_create, on_stats=on_stats)
        if tokens[0].text.upper() == "EXECUTE":
            if len(tokens) < 2:
                raise ValueError()
            if tokens[1].text.upper() == "IMMEDIATE":
                from sqlglot.tokens import TokenType
                if len(tokens) < 3 or tokens[2].token_type != TokenType.STRING:
                    raise ValueError()
                validate_statement(tokens[2].text, depth=depth+1, on_create=on_create, on_stats=on_stats)
            else:
                definitions = dict(p.split("=", 1) for p in prepared.split(",") if "=" in p)
                definition = definitions.get(tokens[1].text)
                if not definition:
                    raise HTTPException(422, "prepared statement definition is required")
                validate_statement(unquote_plus(definition), depth=depth+1, on_create=on_create, on_stats=on_stats)
        # A client can carry prepared definitions into later EXECUTE requests.
        if prepared:
            for part in prepared.split(","):
                if "=" in part:
                    validate_statement(unquote_plus(part.split("=", 1)[1]), depth=depth+1, on_create=on_create, on_stats=on_stats)
    except HTTPException:
        raise
    except (sqlglot.errors.SqlglotError, ValueError, IndexError):
        raise HTTPException(422, "SQL syntax is not supported by the shared gateway") from None


def authorize_initial_creates(query, text, prepared='', catalog='', schema=''):
    """OPA's CreateTable callback cannot distinguish CREATE OR REPLACE.

    Issue a query-local exception only for structured CREATE TABLE IF NOT EXISTS.
    The exception is not supplied by clients and is not carried to later queries.
    """
    from copy import deepcopy
    from sqlglot import exp
    if any(item.get("policies") for item in query.scope.values()) and "STATS" in (text + prepared).upper():
        # Table statistics come from all rows and the real values, past row filters and column masks.
        def refuse():
            raise HTTPException(403, "SHOW STATS is not available while column masks or row filters apply to you")
        validate_statement(text, prepared, on_stats=refuse)
    if "CREATE" not in text.upper() and not prepared:
        return
    scope = deepcopy(query.scope)

    def create(node):
        # CREATE SCHEMA has its own engine authorization. Its unqualified
        # name can otherwise look like a table in the default raw schema.
        if node.args.get('kind') != 'TABLE':
            return
        target = node.this.this if isinstance(node.this, exp.Schema) else node.this
        if not isinstance(target, exp.Table):
            return
        cat, sch = target.catalog or catalog, target.db or schema
        item = scope.get(cat, {})
        # Keep only target identifiers, never SQL text or values. CTAS is both
        # schema creation and a materialization; empty CREATE is schema-only.
        if cat in scope and node.args.get('expression') is not None:
            item.setdefault('lineage_materializations', []).append([sch, target.name])
        if sch not in item.get('append_only_schemas', []):
            return
        if node.args.get('kind') != 'TABLE' or node.args.get('replace') or not node.args.get('exists'):
            raise HTTPException(403, 'append-only schemas permit only CREATE TABLE IF NOT EXISTS for initial setup')
        item.setdefault('append_only_creates', []).append([sch, target.name])

    validate_statement(text, prepared, on_create=create)
    query.scope = scope


def policy_allow(query, action):
    op, resource = action.get("operation"), action.get("resource") or {}
    if op == "ExecuteQuery":
        return True
    if op in {"ViewQueryOwnedBy", "FilterViewQueryOwnedBy", "KillQueryOwnedBy"}:
        return (resource.get("user") or {}).get("user") == query.principal_id
    if op == "SetSystemSessionProperty":
        return resource.get("systemSessionProperty") in SAFE_SYSTEM_PROPERTIES
    if op in {"ExecuteFunction", "FilterFunctions", "CreateViewWithExecuteFunction"}:
        function = resource.get("function") or {}
        schema = function.get("schema") or {}
        return schema.get("catalogName", function.get("catalogName")) == "system" and schema.get("schemaName", function.get("schemaName")) == "builtin"
    if op in {"AccessCatalog", "FilterCatalogs", "ShowSchemas"}:
        catalog = resource.get("catalog")
        name = catalog.get("name") if isinstance(catalog, dict) else catalog
        return name in query.scope or name == "system"
    obj = resource.get("table") or resource.get("schema") or {}
    catalog, schema = obj.get("catalogName"), obj.get("schemaName")
    if catalog == "system" and schema == "jdbc":
        # JDBC metadata (catalogs, schemas, tables, columns) for SQL clients. Trino builds these rows
        # through the Filter* access-control callbacks, so they list only objects the query may see.
        return op in READ
    if catalog == "system" and schema == "metadata":
        # dbt uses these system tables. A mandatory policy row filter below bounds
        # their rows to the query's schema grants, even without a WHERE clause.
        return op in READ and obj.get("tableName", "table_comments") in {"table_comments", "materialized_views"}
    scope = query.scope.get(catalog, {}).get("schemas", {})
    metadata_scope = query.scope.get(catalog, {})
    if metadata_scope.get("system_metadata"):
        if schema == "information_schema":
            return op in READ
        return (op in READ and schema in scope and
                (not obj.get("tableName") or obj["tableName"] in metadata_scope.get("tables", [])))
    if [schema, obj.get("tableName")] in query.scope.get(catalog, {}).get("blocked_relations", []):
        return False
    from .data_policies import protected_base
    if protected_base(query, obj) is not None:
        # Iceberg metadata tables expose partition values and column bounds of masked or filtered tables.
        return False
    if schema == "information_schema" and catalog in query.scope:
        # Trino applies FilterSchemas/FilterTables/FilterColumns to information_schema rows.
        return op in READ
    level = LEVELS.get(scope.get(schema), 0)
    if schema in query.scope.get(catalog, {}).get('append_only_schemas', []):
        # Enforcement is at Trino's OPA boundary, including prepared SQL and
        # direct engine requests. Admin does not bypass the retention contract.
        if op in (WRITE | DDL) - {'InsertIntoTable', 'CreateTable', 'CreateSchema'}:
            return False
        if op == 'CreateTable' and [schema, obj.get('tableName')] not in query.scope[catalog].get('append_only_creates', []):
            return False
    if op in READ:
        return level >= 1
    if op in WRITE:
        return level >= 2
    if op in DDL:
        if level < 3:
            return False
        if op in {"CreateTable","CreateView","CreateMaterializedView","CreateSchema"}:
            primary = query.scope.get(catalog,{}).get("create_schemas")
            if primary is not None and schema not in primary:
                return False
        # A caller must not choose storage locations, aliases, or external metadata.
        properties = obj.get("properties") or {}
        safe_properties = {"format", "format_version", "partitioning", "sorted_by", "compression_codec"}
        if set(properties) - safe_properties:
            return False
        target = action.get("targetResource") or {}
        target = target.get("table") or target.get("schema")
        if target and target.get('schemaName') in query.scope.get(target.get('catalogName'), {}).get('append_only_schemas', []):
            return False
        if target and LEVELS.get(query.scope.get(target.get("catalogName"), {}).get("schemas", {}).get(target.get("schemaName")), 0) < 3:
            return False
        return True
    # Definer views, arbitrary routines/maintenance, grants, authorization transfers,
    # authorization transfers and every new/unreviewed engine action are denied.
    return False


def metadata_filters(query, table):
    if table.get("catalogName") != "system" or table.get("schemaName") != "metadata":
        return []
    if table.get("tableName") not in {"table_comments", "materialized_views"}:
        return [{"expression": "false"}]
    def quote(value):
        return "'" + value.replace("'", "''") + "'"
    clauses = []
    for catalog, scope in query.scope.items():
        schemas = ",".join(quote(schema) for schema in scope["schemas"])
        if schemas:
            clauses.append("(catalog_name=" + quote(catalog) + " AND schema_name IN (" + schemas + "))")
    expression = "(" + " OR ".join(clauses) + ")" if clauses else "false"
    if table.get("tableName") == "table_comments":
        for catalog, scope in query.scope.items():
            for schema, name in scope.get("blocked_relations", []):
                expression += " AND NOT (catalog_name=" + quote(catalog) + " AND schema_name=" + quote(schema) + " AND table_name=" + quote(name) + ")"
    return [{"expression": expression}]
