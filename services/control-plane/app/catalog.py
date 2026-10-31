"""Permission-aware catalog, measured service levels, and provenance-preserving lineage."""
import hashlib
import json
import os
import re
import unicodedata
from . import layers

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import select, delete

from . import rbac
from .models import CatalogAsset, CatalogImport, CatalogRunOutput, LineageEdge, Resource, Workload, Run, DataSource, utcnow


def asset_id(resource_id, key=""):
    return hashlib.sha256((resource_id + "\0" + key).encode()).hexdigest()[:32]


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and not value.tzinfo else value


def timestamp(value):
    try:
        return aware(datetime.fromisoformat(value.replace("Z", "+00:00"))) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def resource_path(db, resource):
    return " / ".join(r.name for r in reversed(rbac.ancestors(db, resource)))


def reindex(asset):
    # Only explicit public metadata, never compiled SQL, connection configuration or run arguments.
    g, a = asset.governance or {}, asset.attributes or {}
    content = " ".join(str(v) for v in [asset.name, asset.path, asset.kind,
        g.get("description", a.get("description", "")), g.get("domain", ""), g.get("steward", ""),
        g.get("classification", "unclassified"), g.get("status", "draft"), " ".join(g.get("tags", [])), a.get("repo", ""),
        a.get("branch", ""), a.get("program", ""), a.get("source", ""),
        a.get("source_kind", ""), " ".join(c.get("name", "") + " " + (c.get("description") or "") for c in a.get("columns", []))]).casefold()
    asset.search_text = "".join(c for c in unicodedata.normalize("NFKD", content) if not unicodedata.combining(c))


def upsert(db, resource, key="", kind=None, name=None, path=None, attributes=None):
    ident = asset_id(resource.id, key)
    asset = db.get(CatalogAsset, ident)
    if not asset:
        dialect = db.bind.dialect.name
        if dialect in ("sqlite", "postgresql"):
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert
            else:
                from sqlalchemy.dialects.sqlite import insert
            db.execute(insert(CatalogAsset).values(id=ident, resource_id=resource.id, kind=kind or resource.kind,
                name=name or resource.name, path=path or "", governance={}, sla={}, attributes={}, search_text="")
                .on_conflict_do_nothing(index_elements=["id"]))
            asset = db.get(CatalogAsset, ident)
        else:
            asset = CatalogAsset(id=ident, resource_id=resource.id, governance={}, sla={}, attributes={})
            db.add(asset)
    if not (asset.governance or {}).get("owner_group_id") and (resource.config or {}).get("owner_group_id"):
        asset.governance = {**(asset.governance or {}), "owner_group_id":resource.config["owner_group_id"]}
    asset.kind = kind or resource.kind
    asset.name = (name or resource.name)[:512]
    asset.path = (path if path is not None else resource_path(db, resource))[:2048]
    if resource.kind == "schema":
        source = db.get(DataSource, resource.object_id)
        if source:
            attributes = {**(attributes or {}), "source": source.name, "source_kind": source.kind}
    if attributes is not None:
        asset.attributes = {**(asset.attributes or {}), **attributes}
    asset.observed_at = utcnow()
    reindex(asset)
    db.flush()
    return asset


def edge(db, producer, source, target, relation, origin, revision=""):
    if source.id == target.id:
        return
    existing = db.scalar(select(LineageEdge).where(LineageEdge.producer_id == producer.id,
        LineageEdge.source_id == source.id, LineageEdge.target_id == target.id,
        LineageEdge.relation == relation, LineageEdge.origin == origin))
    if not existing:
        existing = LineageEdge(producer_id=producer.id, source_id=source.id, target_id=target.id,
                               relation=relation, origin=origin)
        db.add(existing)
    existing.revision, existing.observed_at = revision[:128], utcnow()


def sync_registry(db):
    """Index registered metadata, never query a source or enumerate its data objects."""
    rbac.root(db)
    for ds in db.scalars(select(DataSource)):
        rbac.source_resource(db, ds)
    pubs = list(db.scalars(select(Workload).where(Workload.status != "stopped")))
    for pub in pubs:
        rbac.workload_resource(db, pub)
    resources = list(db.scalars(select(Resource)))
    by_id = {r.id: r for r in resources}
    by_source = {r.name: r for r in resources if r.kind == "source"}
    # Enrich already indexed objects without querying their data sources.
    schemas = {r.id: r for r in resources if r.kind == "schema"}
    for known in db.scalars(select(CatalogAsset).where(CatalogAsset.resource_id.in_(schemas),
            CatalogAsset.attributes["source_kind"].as_string().is_(None))):
        source = db.get(DataSource, schemas[known.resource_id].object_id)
        if source:
            known.attributes = {**known.attributes, "source_kind": source.kind, "source": source.name}
            reindex(known)
    kafka_clusters = {}
    for resource in resources:
        if resource.kind in ('kafka', 'topic') and resource.config.get('deleted'):
            continue
        if resource.kind in ("root", "api-root", "layer", "package-root", "package", "product-root", "product"):
            continue  # Navigation roots are not deployed products.
        if resource.kind == "service" and resource.object_id not in {p.id for p in pubs}:
            continue
        indexed = upsert(db, resource, attributes={"source": by_id[resource.parent_id].name if resource.kind == "schema" and resource.parent_id in by_id else "",
                                      "source_kind": resource.config.get("source_kind", "")})
        if resource.kind == 'topic' and resource.parent_id in by_id:
            parent = by_id[resource.parent_id]
            if not parent.config.get('deleted'):
                if parent.id not in kafka_clusters:
                    kafka_clusters[parent.id] = db.get(CatalogAsset, asset_id(parent.id)) or upsert(db,parent)
                indexed.attributes = {**indexed.attributes, 'source_kind':'kafka'}
                reindex(indexed)
                edge(db, parent, kafka_clusters[parent.id], indexed,
                     'contains', 'resource_hierarchy')
    for pub in pubs:
        resource = by_id[pub.resource_id]
        asset = upsert(db, resource, kind=pub.kind, attributes={"repo": pub.repo.name, "branch": pub.branch,
            "program": pub.program, "runtime": pub.runtime, "status": pub.status,
            "description": (pub.program_def or {}).get("description", "")})
        db.execute(delete(LineageEdge).where(LineageEdge.producer_id == resource.id, LineageEdge.origin == "program_declared"))
        for source_name in (pub.program_def or {}).get("data_sources", []):
            if source_name in by_source:
                edge(db, resource, upsert(db, by_source[source_name]), asset, "uses", "program_declared", pub.deployed_sha or "")
        from .catalog_ingest import dataset
        for field, relation in (("inputs", "reads"), ("outputs", "writes")):
            for reference in ((pub.program_def or {}).get("lineage") or {}).get(field, []):
                data = dataset(db, resource, reference)
                if data:
                    edge(db, resource, data if field == "inputs" else asset,
                         asset if field == "inputs" else data, relation, "program_declared", pub.deployed_sha or "")
    db.flush()


def index_children(db, schema, items, table=None, path=""):
    """Remember metadata returned by an authorized, user-initiated data browser request."""
    if table:
        asset = db.get(CatalogAsset, asset_id(schema.id, "object:" + table))
        if asset:
            columns = {c["name"]: c for c in (asset.attributes or {}).get("columns", [])}
            columns.update({c["name"]: c for c in items})
            asset.attributes = {**asset.attributes, "columns": list(columns.values())}
            reindex(asset)
        return
    parent = resource_path(db, schema)
    for item in items:
        key = item.get("path", item["name"])
        if key.endswith(("__dbt_tmp", "__dbt_backup")):
            continue
        upsert(db, schema, "object:" + key, kind=item["kind"], name=item["name"], path=parent + " / " + key,
               attributes={"object_path": key, "schema": schema.name, "origin": "data_browser"})


def permissions(db, user):
    """Bulk evaluation of the same fixed roles/additive inheritance for a catalog request."""
    cache_key = ("catalog_permissions", user.principal_id, user.username, user.environment_id, user.execution_context)
    if cache_key in db.info:
        return db.info[cache_key]
    user = rbac.resolve_user(db, user)
    resources = {r.id: r for r in db.scalars(select(Resource))}
    grants = {}
    for grant in rbac.role_grants(db, user, resources):
        grants.setdefault(grant.resource_id, []).append(grant.role)
    actions = {}
    recovery = {"assign", "identity", "create", "delete", "edit", "deploy", "read", "execute"}
    from .environments import permits
    for resource in resources.values():
        if not permits(db, user, resource):
            actions[resource.id] = set()
            continue
        roles = rbac.roles_for(resource)
        allowed, seen, current = set(), set(), resource
        while current and current.id not in seen:
            seen.add(current.id)
            for role in grants.get(current.id, []):
                allowed |= roles.get(role, set())
            current = resources.get(current.parent_id)
        if current:
            raise HTTPException(409, "resource cycle detected")
        if user.is_admin and not user.execution_context:
            allowed |= roles.get("Owner",set())
        actions[resource.id] = allowed
    db.info[cache_key] = (resources, actions)
    return resources, actions


def readable_resources(db, user, browsing=False):
    """Resolve permissions once per request, rather than once per catalog entry."""
    resources, actions = permissions(db, user)
    allowed = {rid for rid, values in actions.items() if "read" in values}
    if browsing:
        from .ownership import browsing_scope
        scope = browsing_scope(db, user)
        allowed = {rid for rid in allowed if any("read" in rbac.roles_for(resources[rid]).get(role, set())
                   for role in scope.get(rid, set()))}
    return resources, allowed


def visible_assets(db, user, terms=(), condition=None):
    resources, allowed = readable_resources(db, user, browsing=True)
    stopped = {p.resource_id for p in db.scalars(select(Workload).where(Workload.status == "stopped"))}
    stopped.update(r.id for r in resources.values() if r.kind in ('kafka', 'topic') and r.config.get('deleted'))
    statement = select(CatalogAsset).where(CatalogAsset.resource_id.in_(allowed - stopped))
    if condition is not None:
        statement = statement.where(condition)
    for term in terms:
        statement = statement.where(CatalogAsset.search_text.contains(term, autoescape=True))
    assets = [a for a in db.scalars(statement)
              if not (a.attributes or {}).get("inactive")]
    # Metric dependencies can repeat across hundreds of tests. Fetch their resource
    # IDs together and use the same permission snapshot as the containing entries.
    dependency_ids = {ident for a in assets for ident in a.attributes.get('dependency_asset_ids', [])}
    dependencies = dict(db.execute(select(CatalogAsset.id, CatalogAsset.resource_id).where(
        CatalogAsset.id.in_(dependency_ids))).all()) if dependency_ids else {}
    assets = [a for a in assets if all(dependencies.get(ident) in allowed
              for ident in a.attributes.get('dependency_asset_ids', []))]
    from .layers import info
    for asset in assets:
        asset.catalog_layer = info(db, resources[asset.resource_id])
    return assets, resources


def require_asset(db, ident, user, action="read"):
    asset = db.get(CatalogAsset, ident)
    if asset is None:
        # Objects are indexed lazily (on search). Automation may address a new object by its
        # derived ID first, so index the registry once before deciding it does not exist.
        sync_registry(db)
        db.flush()
        asset = db.get(CatalogAsset, ident)
    resource = db.get(Resource, asset.resource_id) if asset else None
    pub = db.get(Workload, resource.object_id) if resource and resource.kind == "service" and not resource.config.get("service_account") else None
    if not resource or resource.config.get('deleted') or asset.attributes.get("inactive") or (pub and pub.status == "stopped") or not rbac.allowed(db, user, "read", resource):
        raise HTTPException(404, "catalog object not available")
    from .measurements import dependencies_visible
    if not dependencies_visible(db, user, asset.attributes.get('dependency_asset_ids', [])):
        raise HTTPException(404, 'catalog object not available')
    if action != "read":
        rbac.require(db, user, action, resource)
    return asset, resource


def base_service_level(db, asset, resource, now=None):
    now = now or utcnow()
    policy = asset.sla or {}
    if policy.get("freshness_minutes") is None and policy.get("success_rate_percent") is None:
        return {"status": "not_configured", "checks": []}
    runs = []
    if resource.kind == "service" and resource.object_id and (asset.id == asset_id(resource.id) or asset.kind == "program"):
        runs = list(db.scalars(select(Run).where(Run.workload_id == resource.object_id,
            Run.status.in_(("succeeded", "failed", "stopped"))).order_by(Run.created_at.desc())))
        # Docs generation/tests are not data refreshes and cannot repair a freshness breach.
        runs = [r for r in runs if r.kind not in ("app", "airflow") and (r.args or {}).get("command") not in ("docs", "test", "freshness")]
        if asset.kind == "program":
            runs = [r for r in runs if r.program == asset.attributes.get("program")]
    successful = [aware(r.finished_at) for r in runs if r.status == "succeeded" and r.finished_at]
    last_success = max([v for v in [aware(asset.last_success_at), *successful] if v], default=None)
    checks = []
    if policy.get("freshness_minutes") is not None:
        age = (now - last_success).total_seconds() / 60 if last_success else None
        checks.append({"name": "Freshness", "status": "unknown" if age is None else "breached" if age > policy["freshness_minutes"] else "healthy",
            "target": policy["freshness_minutes"], "value": round(age, 1) if age is not None else None, "unit": "minutes"})
    if policy.get("success_rate_percent") is not None:
        cutoff = now - timedelta(hours=policy.get("window_hours", 168))
        recent = [r for r in runs if r.finished_at and aware(r.finished_at) >= cutoff]
        rate = 100 * sum(r.status == "succeeded" for r in recent) / len(recent) if recent else None
        checks.append({"name": "Successful runs", "status": "unknown" if rate is None else "breached" if rate < policy["success_rate_percent"] else "healthy",
            "target": policy["success_rate_percent"], "value": round(rate, 2) if rate is not None else None, "unit": "%", "samples": len(recent)})
    statuses = {c["status"] for c in checks}
    status = "breached" if "breached" in statuses else "unknown" if "unknown" in statuses else "healthy"
    return {"status": status, "checks": checks, "last_success_at": last_success}


def service_level(db, asset, resource, now=None):
    from .sla_monitor import evaluate
    return evaluate(db, asset, resource, now)


def finished_run(db, run):
    """Successful output observations, derived from the orchestrator's terminal run status."""
    if run.status != "succeeded" or not run.finished_at or (run.args or {}).get("command") in ("docs", "test", "freshness"):
        return
    if not run.workload_id:
        return
    pub = db.get(Workload, run.workload_id)
    if not pub or not pub.resource_id:
        return
    outputs = list(db.scalars(select(CatalogRunOutput).where(CatalogRunOutput.run_id == run.id)))
    for link in outputs:
        asset = db.get(CatalogAsset, link.asset_id)
        from .measurements import can_attest
        if asset and not can_attest(db,db.get(Resource,asset.resource_id),access_snapshot=run.access_snapshot or {}):
            continue
        if asset and (not asset.last_success_at or aware(run.finished_at) > aware(asset.last_success_at)):
            asset.last_success_at = run.finished_at
        if asset:
            from .measurements import dataset_observation
            dataset_observation(db, asset, run.finished_at, 'run', run.id, run_id=run.id, revision=run.commit_sha or '')


def run_definition(pub, run):
    if not run or run.program == pub.program:
        return pub.program_def
    from . import deploying
    programs, _ = deploying.programs_at(deploying.get_git(), pub.repo, run.commit_sha or pub.deployed_sha)
    program = programs.get(run.program)
    if not program or program.kind != "dbt":
        raise HTTPException(422, "run has no dbt program definition at its pinned commit")
    return program.model_dump(mode="json", by_alias=True)


def harvest_artifacts(db):
    """Backfill completed deployed dbt artifacts from the platform volume; no source queries."""
    from .config import get_settings
    from .catalog_ingest import dbt, imported
    from .git import GitError
    runs = list(db.scalars(select(Run).where(Run.workload_id.is_not(None), Run.kind == "dbt",
        Run.status.in_(("succeeded", "failed"))).order_by(Run.finished_at.desc()).limit(100)))
    for run in reversed(runs):
        pub = db.get(Workload, run.workload_id)
        if not pub or pub.status == "stopped" or not pub.resource_id:
            continue
        if db.scalar(select(CatalogImport).where(CatalogImport.resource_id == pub.resource_id,
                CatalogImport.origin == "dbt_quality_v1", CatalogImport.scope == run.id)):
            continue
        from .artifacts import for_run
        try:
            store = for_run(db, run)
        except HTTPException:
            continue  # its layer's artifact storage is not reachable right now; retried next pass
        root = f"runs/{run.id}/outputs"
        if store.read(root + "/manifest.json", 1) is None:
            continue
        try:
            with db.begin_nested():
                def read(filename):
                    limit = 8 * 1024 * 1024
                    data = store.read(root + "/" + filename, limit + 1)
                    if data is None:
                        return None
                    if len(data) > limit:
                        raise ValueError("artifact too large")
                    return json.loads(data)
                dbt(db, pub, read("manifest.json"), read("catalog.json"), read("run_results.json"), read("sources.json"),
                    run.commit_sha or "", run.program, run_definition(pub, run), run_id=run.id, access_snapshot=run.access_snapshot or {})
                imported(db, db.get(Resource, pub.resource_id), "dbt_artifacts", run.commit_sha or "", 0, 0, run.id)
                imported(db, db.get(Resource, pub.resource_id), "dbt_quality_v1", run.commit_sha or "", 0, 0, run.id)
        except (ValueError, TypeError, KeyError, AttributeError, OSError, HTTPException, GitError):
            import logging
            logging.getLogger(__name__).warning("Could not import dbt metadata for run %s", run.id)


def successful_query(db, query):
    """Engine-observed, authorized table operations; called only after query completion."""
    from .catalog_ingest import dataset
    from .sql_access import catalog_name, SQL_KINDS
    pub = db.get(Workload, query.workload_id) if query.workload_id else None
    if not pub or not pub.resource_id:
        return
    producer = db.get(Resource, pub.resource_id)
    run = db.get(Run, query.run_id) if query.run_id else None
    program = upsert(db, producer, kind=pub.kind) if not run or run.program == pub.program else upsert(db, producer,
        "program:" + (run.program or run.entrypoint), kind="program", name=run.program or run.entrypoint,
        attributes={"repo": pub.repo.name, "branch": pub.branch, "program": run.program or run.entrypoint})
    if program.id != asset_id(producer.id):
        edge(db, producer, upsert(db, producer, kind=pub.kind), program, "contains", "trino_runtime", query.id)
    for item in (query.lineage or {}).values():
        source = next((s for s in db.scalars(select(DataSource)) if s.kind in SQL_KINDS and catalog_name(s) == item["catalog"]), None)
        if not source:
            continue
        data = dataset(db, producer, {"source": source.name, "schema": item["schema"], "name": item["table"]})
        if data:
            for relation in item["relations"]:
                edge(db, producer, data if relation == "reads" else program, program if relation == "reads" else data,
                     relation, "trino_runtime", query.id)
                if relation == "writes":
                    data.last_success_at = utcnow()
                    from .measurements import dataset_observation
                    dataset_observation(db, data, data.last_success_at, 'trino', query.id, run_id=query.run_id or '')


def asset_out(db, asset, resource, user):
    from . import classification
    from .models import AppGroup
    _, actions = permissions(db, user)
    permitted = actions.get(resource.id, set())
    group = db.get(AppGroup, (asset.governance or {}).get("owner_group_id")) if (asset.governance or {}).get("owner_group_id") else None
    return {**layers.info(db, resource), "id": asset.id, "name": asset.name, "kind": asset.kind, "path": asset.path,
        "resource": {"id": resource.id, "name": resource.name, "kind": resource.kind, "parent_id": resource.parent_id,
            "object_id": resource.object_id, "config": resource.config or {}, "actions": sorted(permitted)}, "attributes": asset.attributes,
        "governance": asset.governance, "sla": asset.sla, "service_level": service_level(db, asset, resource),
        "classification": classification.describe(db, asset),
        "owner_group_name": group.name if group else None,
        "observed_at": asset.observed_at,
        "can_edit": bool(permitted & {"edit", "write", "ddl"}), "can_govern": "assign" in permitted}


def lineage_category(source, target, relation):
    if relation == "creates":
        return "schema"
    if any((getattr(asset, "governance", None) or {}).get("lineage_role") == "operational"
           for asset in (source, target)):
        return "operational"
    return "data"


def data_flow_edges(edges, assets, focus, detailed=False):
    """A data-flow projection, preserving the original evidence for each connection.

    Logical dbt model dependencies replace the duplicate model -> table -> model path
    within a project. Physical datasets remain at ingestion/application/project boundaries.
    Control flow, tests, containers and connection-level source usage belong in Full graph.
    """
    transforms = {a.id for a in assets.values() if a.kind in {"dbt_model", "dbt_seed", "dbt_snapshot"}}
    projects = {assets[i].resource_id for i in transforms}
    dependencies = {(e.source_id, e.target_id) for e in edges if e.relation == "depends_on"
                    and e.source_id in transforms and e.target_id in transforms}
    writers = {}
    for e in edges:
        if e.relation == "writes" and e.source_id in transforms:
            writers.setdefault(e.target_id, set()).add(e.source_id)
    project = assets[focus].resource_id if assets[focus].kind == "dbt" else None
    project_models = {i for i in transforms if assets[i].resource_id == project}
    internal_tables = {table for table, producers in writers.items() if producers & project_models}
    model_consumers = {a for a, b in dependencies if a in project_models and b in project_models}
    boundary_tables = {table for table, producers in writers.items()
                       if producers & (project_models - model_consumers)}
    boundary_tables.update(e.source_id for e in edges if e.relation == "reads"
                          and e.source_id in internal_tables and assets[e.target_id].resource_id != project)
    grouped = {}
    file_writers, file_readers = {}, {}
    for e in edges:
        if e.relation == "writes" and assets[e.target_id].kind == "file":
            file_writers.setdefault(e.target_id, set()).add(e.source_id)
        if e.relation == "reads" and assets[e.source_id].kind == "file":
            file_readers.setdefault(e.source_id, set()).add(e.target_id)
    families, eligible = {}, set()
    for ident, writers_for_file in file_writers.items():
        readers_for_file = file_readers.get(ident, set()) - writers_for_file
        if readers_for_file and (getattr(assets[ident], "governance", None) or {}).get("lineage_role") != "operational":
            resource = assets[ident].resource_id
            families.setdefault(resource, set()).add((frozenset(writers_for_file), frozenset(readers_for_file)))
            eligible.add(ident)

    def endpoint(asset):
        # A landing namespace is the dataset boundary in Flow. Per-file nodes
        # remain available in Full and when inspecting an individual file.
        namespace = asset_id(asset.resource_id)
        # Never merge unrelated pipelines just because they share storage.
        if (asset.id in eligible and asset.id != focus and namespace != focus and namespace in assets
                and len(families.get(asset.resource_id, ())) == 1):
            return namespace
        return asset.id

    for e in edges:
        a, b = assets[e.source_id], assets[e.target_id]
        if e.relation not in {"reads", "writes", "depends_on", "creates"}:
            continue
        if any(n.kind.startswith("airflow") or n.kind in {"dbt_test", "dbt_source", "dbt_exposure", "dbt_metric", "dbt_semantic_model"} for n in (a, b)):
            continue
        if e.relation == "depends_on" and (a.id, b.id) not in dependencies:
            continue
        if any(n.kind == "dbt" and n.resource_id in projects for n in (a, b)):
            continue
        if e.relation == "reads" and b.id in transforms and any((writer, b.id) in dependencies for writer in writers.get(a.id, ())):
            continue
        if not detailed and project_models and e.relation == "writes" and a.id in project_models and b.id in internal_tables - boundary_tables:
            continue
        key = (endpoint(a), endpoint(b), e.relation)
        if key[0] == key[1]:
            continue
        grouped.setdefault(key, []).append(e)
    result = []
    for (source, target, relation), evidence in grouped.items():
        # Prefer manifest definitions for transformations; retain every observation as evidence.
        evidence.sort(key=lambda e: (e.origin != "dbt_manifest", e.origin != "program_declared", e.id))
        primary = evidence[0]
        result.append({"id": primary.id, "source": source, "target": target,
            "relation": relation, "origin": primary.origin, "revision": primary.revision,
            "category": lineage_category(assets[source], assets[target], relation),
            "control": lineage_category(assets[source], assets[target], relation) != "data",
            "observed_at": max(aware(e.observed_at) for e in evidence),
            "evidence": [{"id": e.id, "origin": e.origin, "revision": e.revision, "observed_at": e.observed_at} for e in evidence]})
    return result


def successful_api_call(db, api, operation, user=None, workload=None, observed_at=None, revision=""):
    """Record actual managed API consumption, never infer it from permission grants."""
    from .apis import api_resource
    from .environments import resource_environment
    if workload is None and user:
        context = user.execution_context
        if context.startswith("run:"):
            run = db.get(Run, context[4:])
            workload = db.get(Workload, run.workload_id) if run and run.workload_id else None
            revision = run.commit_sha or "" if run else ""
        elif context.startswith("workload:"):
            workload = db.get(Workload, context[len("workload:"):])
    if not workload or workload.status == "stopped":
        return
    producer = rbac.workload_resource(db, workload)
    upstream = api_resource(db, api)
    if resource_environment(db, producer) != resource_environment(db, upstream):
        return
    source = upsert(db, upstream, kind="api", attributes={"direction":api.direction,"protocol":api.protocol})
    target = upsert(db, producer, kind=workload.kind)
    edge(db, producer, source, target, "reads", "api_runtime", revision or workload.deployed_sha or "")
    if observed_at:
        link = db.scalar(select(LineageEdge).where(LineageEdge.producer_id == producer.id,
            LineageEdge.source_id == source.id, LineageEdge.target_id == target.id,
            LineageEdge.relation == "reads", LineageEdge.origin == "api_runtime"))
        link.observed_at = observed_at


def harvest_api_calls(db):
    """Recover historical managed-identity calls from audit evidence without rerunning jobs.

    A managed workload principal identifies its producer exactly. Shared service-account
    and human calls cannot be attributed retrospectively and are deliberately not inferred.
    """
    from .models import ApiDefinition, AuditEvent
    pubs = {"service:" + p.service_principal_id: p for p in db.scalars(select(Workload)) if p.service_principal_id and p.status != "stopped"}
    apis = {a.id:a for a in db.scalars(select(ApiDefinition))}
    seen = set()
    for event in db.scalars(select(AuditEvent).where(AuditEvent.action == "api.egress").order_by(AuditEvent.id.desc())):
        key = (event.actor, event.target)
        if key in seen or event.actor not in pubs or event.target not in apis or not 200 <= (event.detail or {}).get("status", 0) < 300:
            continue
        seen.add(key)
        successful_api_call(db, apis[event.target], None, workload=pubs[event.actor], observed_at=event.at, revision=f"audit:{event.id}")
    db.flush()


def orchestration_overlay(edges, assets, selected, links, distances):
    """Attach only schedulers of selected data work; never traverse through control edges."""
    parents = {}
    truncated = False
    selected_data = set(selected)
    memberships = [e for e in edges if e.relation == "contains"]
    for trigger in sorted(edges, key=lambda e: e.id):
        if trigger.relation != "orchestrates" or assets[trigger.source_id].kind != "airflow_task":
            continue
        target = trigger.target_id
        models = {e.target_id for e in memberships if e.source_id == target and e.target_id in selected_data
                  and assets[e.target_id].kind in ("dbt_model", "dbt_seed", "dbt_snapshot")}
        if target not in selected_data and not models:
            continue
        dag_links = [e for e in memberships if e.target_id == trigger.source_id and assets[e.source_id].kind == "airflow_dag"]
        additions = {trigger.source_id, target} | {e.source_id for e in dag_links}
        if len(selected | additions) > 150:
            truncated = True
            continue
        selected.update(additions)
        for ident in additions:
            distances.setdefault(ident, distances.get(target, 0))
        for model in models:
            parents[model] = target
        for e in dag_links:
            parents[trigger.source_id] = e.source_id
        links.append({"id": trigger.id, "source": trigger.source_id, "target": target, "relation": "triggers",
                      "origin": trigger.origin, "revision": trigger.revision, "observed_at": trigger.observed_at, "control": True})
    # Membership and task ordering connect only already selected scheduler objects.
    for e in edges:
        membership = e.relation == "contains" and parents.get(e.target_id) == e.source_id
        ordering = e.relation == "depends_on" and assets[e.source_id].kind == assets[e.target_id].kind == "airflow_task"
        if (membership or ordering) and e.source_id in selected and e.target_id in selected:
            links.append({"id":e.id,"source":e.source_id,"target":e.target_id,"relation":e.relation,
                          "origin":e.origin,"revision":e.revision,"observed_at":e.observed_at,"control":True})
    return parents, truncated


def hierarchy_parents(assets, edges, focus):
    """Intrinsic containment only; resource permissions were evaluated before this step."""
    parents = {}
    for e in edges:
        if e.relation != "contains":
            continue
        parent, child = assets[e.source_id], assets[e.target_id]
        if ((parent.kind == "kafka" and child.kind == "topic")
                or (parent.kind == "dbt" and child.kind in {"dbt_model", "dbt_seed", "dbt_snapshot"})
                or (parent.kind == "airflow" and child.kind == "airflow_dag")
                or (parent.kind == "airflow_dag" and child.kind == "airflow_task")):
            parents[child.id] = parent.id
    for child in assets.values():
        root = assets.get(asset_id(child.resource_id))
        if not root or root.id == child.id:
            continue
        if ((root.kind == "schema" and child.kind in {"table", "view", "materialized_view", "file", "folder"})
                or (root.kind == "dbt" and child.kind in {"dbt_model", "dbt_seed", "dbt_snapshot"})
                or (root.kind == "airflow" and child.kind == "airflow_dag")):
            parents.setdefault(child.id, root.id)
    if assets[focus].kind == "source":
        for child in assets.values():
            if child.kind == "schema" and (child.attributes or {}).get("source") == assets[focus].name:
                parents[child.id] = focus
    return parents


def attach_hierarchy(assets, edges, selected, distances, focus):
    """Add permitted ancestors after scoping; containment never broadens data traversal."""
    parents = hierarchy_parents(assets, edges, focus)
    result, truncated = set(), False
    for ident in sorted(selected, key=lambda i: (i != focus, abs(distances.get(i, 0)), i)):
        branch, current = {ident}, ident
        while current in parents and parents[current] not in branch:
            current = parents[current]
            branch.add(current)
        if len(result | branch) > 150:
            truncated = True
            continue
        result.update(branch)
        for ancestor in branch:
            distances.setdefault(ancestor, distances.get(ident, 0))
    selected.clear()
    selected.update(result)
    return {child: parent for child, parent in parents.items() if child in selected and parent in selected}, truncated


def graph(db, user, focus, direction="both", depth=3, origin="", since=None, view="full", orchestration=False, operational=False, hierarchy=False):
    assets, resources = visible_assets(db, user)
    by_id = {a.id: a for a in assets}
    if focus not in by_id:
        raise HTTPException(404, "catalog object not available")
    allowed_resources = {a.resource_id for a in assets}
    # Filter both endpoints AND the producer before traversal, so hidden nodes cannot bridge objects.
    membership_edges = [e for e in db.scalars(select(LineageEdge)) if e.source_id in by_id and e.target_id in by_id
                        and e.producer_id in allowed_resources]
    edges = [e for e in membership_edges if (not origin or e.origin == origin)
             and (not since or aware(e.observed_at) >= aware(since))]
    links = data_flow_edges(edges, by_id, focus, detailed=hierarchy) if view == "flow" else [
        {"id":e.id,"source":e.source_id,"target":e.target_id,"relation":e.relation,
         "origin":e.origin,"revision":e.revision,"observed_at":e.observed_at,
         "category":lineage_category(by_id[e.source_id], by_id[e.target_id], e.relation),
         "control":lineage_category(by_id[e.source_id], by_id[e.target_id], e.relation) != "data"} for e in edges]
    auxiliary = [e for e in links if e.get("category") in {"operational", "schema"}] if view == "flow" else []
    if view == "flow":
        # Operational links must not bridge unrelated data paths. A directly inspected
        # operational object retains its incident links and their upstream/downstream context.
        auxiliary_ids = {e["id"] for e in auxiliary}
        links = [e for e in links if e["id"] not in auxiliary_ids or focus in (e["source"], e["target"])]
    project_models = {a.id for a in assets if a.kind in {"dbt_model", "dbt_seed", "dbt_snapshot"}
                      and a.resource_id == by_id[focus].resource_id} if by_id[focus].kind == "dbt" and view == "flow" else set()
    # A project contains its model graph. Do not force runtime table reads/writes
    # through the container, which creates cycles around its own materializations.
    boundaries = {e["target"] for e in links if e["source"] in project_models and e["relation"] == "writes"}
    members = set(project_models)
    if by_id[focus].kind == 'kafka':
        members.update(e.target_id for e in membership_edges if e.source_id == focus and e.relation == 'contains')
    if by_id[focus].kind in {"schema", "source"}:
        boundary_ids = {by_id[focus].resource_id}
        if by_id[focus].kind == "source":
            boundary_ids.update(r.id for r in resources.values() if r.parent_id == by_id[focus].resource_id and r.kind == "schema")
        members.update(a.id for a in assets if a.resource_id in boundary_ids and a.id != focus
                       and a.kind in {"table", "view", "materialized_view", "file"})
    if hierarchy and by_id[focus].kind in {"airflow", "airflow_dag", "airflow_task"}:
        intrinsic = hierarchy_parents(by_id, membership_edges, focus)
        members.update(child for child, parent in intrinsic.items() if parent == focus)
        if by_id[focus].kind == "airflow_task":
            members.add(focus)
        if by_id[focus].kind == "airflow":
            members.update(child for child, parent in intrinsic.items() if parent in members)
        for e in edges:
            if e.relation == "orchestrates" and e.source_id in members:
                links.append({"id":e.id,"source":e.source_id,"target":e.target_id,"relation":"triggers",
                              "origin":e.origin,"revision":e.revision,"observed_at":e.observed_at,"control":True})
                boundaries.add(e.target_id)
        for e in edges:
            if (e.relation == "depends_on" and by_id[e.source_id].kind == by_id[e.target_id].kind == "airflow_task"
                    and by_id[e.source_id].resource_id == by_id[focus].resource_id):
                links.append({"id":e.id,"source":e.source_id,"target":e.target_id,"relation":"depends_on",
                              "origin":e.origin,"revision":e.revision,"observed_at":e.observed_at,"control":True})
    upstream, downstream = {}, {}
    for e in links:
        upstream.setdefault(e["target"], set()).add(e["source"])
        downstream.setdefault(e["source"], set()).add(e["target"])
    initial = sorted(members | boundaries)
    truncated = len(initial) > 149
    selected = {focus} | set(initial[:149])
    members &= selected
    project_models &= selected
    distances = {ident: 0 for ident in selected}
    # Walk each direction independently. Changing direction at a shared ancestor
    # includes its other consumers, which are not ancestors/descendants of focus.
    for sign, neighbors in ((-1, upstream), (1, downstream)):
        if (sign < 0 and direction == "downstream") or (sign > 0 and direction == "upstream"):
            continue
        frontier = members or {focus}
        seen = set(frontier)
        for step in range(1, depth + 1):
            next_ids = set().union(*(neighbors.get(i, set()) for i in frontier)) - seen
            available = 150 - len(selected)
            additions = sorted(next_ids - selected)
            if len(additions) > available:
                next_ids = (next_ids & selected) | set(additions[:available])
                truncated = True
            selected.update(next_ids)
            for ident in next_ids:
                distances.setdefault(ident, sign * step)
            seen.update(next_ids)
            frontier = next_ids
            if not frontier:
                break
    links = [e for e in links if e["source"] in selected and e["target"] in selected]
    if operational and view == "flow":
        # Attach one-hop context to the already scoped data graph, without walking
        # through checkpoints to other consumers or bypassing permission filtering.
        visible = set(selected)
        included_ids = {e["id"] for e in links}
        for e in auxiliary:
            if e["id"] in included_ids:
                continue
            if ((e["target"] in visible and direction != "downstream")
                    or (e["source"] in visible and direction != "upstream")):
                additions = {e["source"], e["target"]} - selected
                if len(selected) + len(additions) > 150:
                    truncated = True
                    continue
                selected.update(additions)
                for ident in additions:
                    distances[ident] = distances.get(e["target"], 0) - 1 if ident == e["source"] else distances.get(e["source"], 0) + 1
                links.append(e)
    parents = {ident: focus for ident in members}
    if orchestration and view == "flow":
        overlay_parents, overlay_truncated = orchestration_overlay(edges, by_id, selected, links, distances)
        parents.update(overlay_parents)
        truncated = truncated or overlay_truncated
    if hierarchy:
        parents, hierarchy_truncated = attach_hierarchy(by_id, membership_edges, selected, distances, focus)
        truncated = truncated or hierarchy_truncated
        links = [e for e in links if e["source"] in selected and e["target"] in selected]
    child_counts = {}
    for parent in parents.values():
        child_counts[parent] = child_counts.get(parent, 0) + 1
    truncated = truncated or len(links) > 400
    return {"nodes": [{**asset_out(db, by_id[i], resources[by_id[i].resource_id], user), "distance": distances[i],
                       **({"lineage_parent_id": parents[i]} if i in parents else {}),
                       **({"lineage_child_count": child_counts[i]} if i in child_counts else {}),
                       "upstream_count": len(upstream.get(i, ())), "downstream_count": len(downstream.get(i, ()))} for i in sorted(selected)],
        "edges": links[:400],
        "truncated": truncated}
