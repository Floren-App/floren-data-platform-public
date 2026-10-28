"""Import native dbt and Airflow metadata without executing code or fetching external URIs."""
from urllib.parse import urlsplit, unquote

from fastapi import HTTPException
from sqlalchemy import select, delete, func

from . import catalog as c
from .models import CatalogAsset, CatalogImport, DataSource, LineageEdge, Resource, Workload, utcnow


def dataset(db, producer, reference):
    """Resolve only registered schema boundaries. URIs are identifiers, never connections."""
    ref = dict(reference)
    if ref.get("uri"):
        parsed = urlsplit(ref["uri"])
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return None  # Credentials and signed URLs must never enter the catalog.
        parts = [unquote(p) for p in parsed.path.split("/") if p]
        if parsed.scheme == "fdp" and len(parts) >= 2:
            ref = {"source": parsed.hostname, "schema": parts[0], "name": "/".join(parts[1:])}
        else:
            return None
    source = db.scalar(select(DataSource).where(DataSource.name == ref.get("source", "")))
    if not source or not ref.get("schema") or not ref.get("name"):
        return None
    schema = db.scalar(select(Resource).where(Resource.kind == "schema", Resource.object_id == source.id,
                                              Resource.name == ref["schema"]))
    if not schema:
        return None
    from .environments import resource_environment
    from .rbac import source_resource
    if resource_environment(db, producer) != resource_environment(db, source_resource(db, source)):
        return None
    name = str(ref["name"])[:512]
    if name.endswith(("__dbt_tmp", "__dbt_backup")):
        return None
    return c.upsert(db, schema, "object:" + name, kind="file" if source.kind == "s3" else "table",
        name=name, path=c.resource_path(db, schema) + " / " + name,
        attributes={"source": source.name, "schema": schema.name, "object_path": name})


def imported(db, producer, origin, revision, nodes, edges, scope=""):
    row = db.scalar(select(CatalogImport).where(CatalogImport.resource_id == producer.id,
                                               CatalogImport.origin == origin, CatalogImport.scope == scope))
    if not row:
        row = CatalogImport(resource_id=producer.id, origin=origin, scope=scope)
        db.add(row)
    row.revision, row.nodes, row.edges, row.observed_at = revision[:128], nodes, edges, utcnow()


def dbt(db, pub, manifest, catalog=None, results=None, freshness=None, revision="", program_name=None, definition=None, run_id='', observation_user=None, access_snapshot=None):
    producer = db.get(Resource, pub.resource_id)
    origin = "dbt_manifest"
    program_name = program_name or pub.program
    definition = definition or pub.program_def
    old_ids = set(db.scalars(select(CatalogAsset.id).where(CatalogAsset.resource_id == producer.id,
        CatalogAsset.attributes["dbt_program"].as_string() == program_name)))
    if old_ids:
        db.execute(delete(LineageEdge).where(LineageEdge.producer_id == producer.id, LineageEdge.origin == origin,
            LineageEdge.source_id.in_(old_ids) | LineageEdge.target_id.in_(old_ids)))
        for old in db.scalars(select(CatalogAsset).where(CatalogAsset.id.in_(old_ids))):
            old.attributes = {**old.attributes, "inactive": True}
    definitions = {}
    for key in ("sources", "nodes", "exposures", "metrics", "semantic_models"):
        definitions.update(manifest.get(key) or {})
    if len(definitions) > 10000:
        raise HTTPException(422, "dbt import exceeds 10000 nodes")
    if not definitions:
        raise HTTPException(422, "manifest must contain dbt nodes or sources")
    catalog_nodes = {**(catalog or {}).get("nodes", {}), **(catalog or {}).get("sources", {})}
    result_by_id = {r.get("unique_id"): r for r in (results or {}).get("results", [])}
    sources = list(db.scalars(select(DataSource)))
    default_source = (definition.get("dbt") or {}).get("data_source")
    declared = set(definition.get("data_sources", [])) | {default_source}
    logical, physical = {}, {}
    workload = c.upsert(db, producer, kind=pub.kind) if program_name == pub.program else c.upsert(db, producer,
        "program:" + program_name, kind="program", name=program_name,
        attributes={"repo": pub.repo.name, "branch": pub.branch, "program": program_name})
    prefix = "dbt:" if program_name == pub.program else "dbt:" + program_name + ":"
    if program_name != pub.program:
        c.edge(db, producer, c.upsert(db, producer, kind=pub.kind), workload, "contains", origin, revision)
    count = 0
    for uid, node in definitions.items():
        if not isinstance(node, dict):
            raise HTTPException(422, "invalid dbt node")
        kind = str(node.get("resource_type", uid.split(".")[0]))[:24]
        columns = node.get("columns") or {}
        observed_columns = catalog_nodes.get(uid, {}).get("columns") or {}
        column_names = sorted(set(columns) | set(observed_columns))[:1000]
        result = result_by_id.get(uid, {})
        attributes = {"origin": origin, "dbt_program": program_name, "inactive": False, "unique_id": uid, "description": str(node.get("description", ""))[:10000],
            "repo": pub.repo.name, "branch": pub.branch, "dbt_type": kind,
            "materialized": (node.get("config") or {}).get("materialized", ""),
            "columns": [{"name": name, "data_type": observed_columns.get(name, {}).get("type", columns.get(name, {}).get("data_type", "")),
                         "description": str(columns.get(name, {}).get("description", ""))[:2000]} for name in column_names],
            "test_status": result.get("status"), "execution_seconds": result.get("execution_time")}
        asset = c.upsert(db, producer, prefix + uid, kind="dbt_" + kind,
            name=str(node.get("name", uid)), path=c.resource_path(db, producer) + " / " + uid, attributes=attributes)
        logical[uid] = asset
        c.edge(db, producer, workload, asset, "contains", origin, revision); count += 1
        database, schema = node.get("database"), node.get("schema")
        candidates = [s for s in sources if s.name in declared and database in (s.name, s.config.get("catalog"), s.config.get("database"))]
        if not candidates and not database:
            candidates = [s for s in sources if s.name == default_source]
        relation = None
        if len(candidates) == 1 and schema and kind in ("model", "source", "seed", "snapshot") and attributes["materialized"] != "ephemeral":
            relation = dataset(db, producer, {"source": candidates[0].name, "schema": schema,
                "name": node.get("alias") or node.get("identifier") or node.get("name")})
        if relation:
            physical[uid] = relation
            # A source is consumed by dbt; a model/seed/snapshot produces a relation.
            c.edge(db, producer, relation if kind == "source" else asset,
                   asset if kind == "source" else relation, "reads" if kind == "source" else "writes", origin, revision)
            count += 1
            # Imported metadata does not overwrite an operator's governed descriptions/tags.
            relation.attributes = {**relation.attributes, "columns": attributes["columns"]}
            c.reindex(relation)
        # Only actual successful materializations count as data refreshes, never docs/tests.
        if result.get("status") == "success" and kind in ("model", "seed", "snapshot"):
            completed = [c.timestamp(t.get("completed_at")) for t in result.get("timing", []) if t.get("name") == "execute"]
            success_at = max([t for t in completed if t and t <= utcnow()], default=None)
            if success_at:
                for refreshed in [asset, *([relation] if relation else [])]:
                    from .measurements import can_attest
                    if not can_attest(db,db.get(Resource,refreshed.resource_id),observation_user,access_snapshot):
                        continue
                    if not refreshed.last_success_at or success_at > c.aware(refreshed.last_success_at):
                        refreshed.last_success_at = success_at
                    from .measurements import dataset_observation
                    dataset_observation(db, refreshed, success_at, 'dbt', str((results or {}).get('metadata', {}).get('invocation_id', '')) + uid + success_at.isoformat(), run_id=run_id, revision=revision)
    for uid, node in definitions.items():
        for dependency in (node.get("depends_on") or {}).get("nodes", []):
            if dependency in logical:
                c.edge(db, producer, logical[dependency], logical[uid], "depends_on", origin, revision); count += 1
                if dependency in physical and node.get("resource_type") != "source":
                    c.edge(db, producer, physical[dependency], logical[uid], "reads", origin, revision); count += 1
    for result in (freshness or {}).get("results", []):
        asset = physical.get(result.get("unique_id"))
        loaded = c.timestamp(result.get("max_loaded_at"))
        from .measurements import can_attest
        if asset and loaded and loaded <= utcnow() and can_attest(db,db.get(Resource,asset.resource_id),observation_user,access_snapshot):
            asset.last_success_at = max(c.aware(asset.last_success_at) or loaded, loaded)
            from .measurements import dataset_observation
            dataset_observation(db, asset, loaded, 'dbt_source_freshness', result.get('unique_id', '') + loaded.isoformat(), kind='watermark', run_id=run_id, revision=revision)
            checked = c.timestamp((freshness or {}).get('metadata', {}).get('generated_at'))
            if checked:
                dataset_observation(db, asset, checked, 'dbt_source_freshness', result.get('unique_id', '') + checked.isoformat(), kind='checked', run_id=run_id, revision=revision)
    from .measurements import dbt_results
    dbt_results(db, logical, physical, definitions, results, revision, run_id, producer.id, program_name, observation_user, access_snapshot)
    imported(db, producer, origin, revision, len(logical) + len(physical), count, program_name)
    db.flush()


def airflow(db, pub, payload, revision=""):
    producer = db.get(Resource, pub.resource_id)
    dag_id, tasks = payload["dag_id"], payload["tasks"]
    origin = "airflow_dag"
    dag_key = "airflow:" + dag_id
    # Replace this DAG's snapshot, leaving other DAGs in the deployment intact.
    old_ids = set(db.scalars(select(CatalogAsset.id).where(CatalogAsset.resource_id == producer.id,
        CatalogAsset.attributes["dag_id"].as_string() == dag_id)))
    if old_ids:
        db.execute(delete(LineageEdge).where(LineageEdge.producer_id == producer.id,
            LineageEdge.origin == origin, (LineageEdge.source_id.in_(old_ids) | LineageEdge.target_id.in_(old_ids))))
        for old in db.scalars(select(CatalogAsset).where(CatalogAsset.id.in_(old_ids))):
            old.attributes = {**old.attributes, "inactive": True}
    workload = c.upsert(db, producer, kind="airflow")
    dag = c.upsert(db, producer, dag_key, kind="airflow_dag", name=dag_id,
        path=c.resource_path(db, producer) + " / " + dag_id,
        attributes={"origin": origin, "inactive": False, "dag_id": dag_id, "schedule": payload.get("schedule", ""), "branch": pub.branch})
    c.edge(db, producer, workload, dag, "contains", origin, revision)
    nodes = {}
    for task in tasks:
        tid = task["task_id"]
        nodes[tid] = c.upsert(db, producer, dag_key + ":task:" + tid, kind="airflow_task", name=tid,
            path=dag.path + " / " + tid, attributes={"origin": origin, "inactive": False, "dag_id": dag_id,
                "operator": task.get("operator", ""), "program": task.get("program") or "", "branch": pub.branch})
        c.edge(db, producer, dag, nodes[tid], "contains", origin, revision)
    for task in tasks:
        asset = nodes[task["task_id"]]
        for upstream in task.get("upstream_task_ids", []):
            if upstream in nodes:
                c.edge(db, producer, nodes[upstream], asset, "depends_on", origin, revision)
        for field, relation in (("inlets", "reads"), ("outlets", "writes")):
            for ref in task.get(field, []):
                data = dataset(db, producer, ref)
                if data:
                    c.edge(db, producer, data if field == "inlets" else asset,
                           asset if field == "inlets" else data, relation, origin, revision)
        target = None
        if task.get("workload"):
            target = db.scalar(select(Workload).where(Workload.slug == task["workload"], Workload.status != "stopped"))
        if target:
            from .environments import resource_environment
            if resource_environment(db, producer) != resource_environment(db, db.get(Resource, target.resource_id)):
                target = None
        if target:
            c.edge(db, producer, asset, c.upsert(db, db.get(Resource, target.resource_id), kind=target.kind), "orchestrates", origin, revision)
        elif task.get("program"):
            # Same-repository task programs are still lineage objects even when not independently deployed.
            program = c.upsert(db, producer, "program:" + task["program"], kind="program", name=task["program"],
                path=c.resource_path(db, producer) + " / " + task["program"],
                attributes={"repo": pub.repo.name, "branch": pub.branch, "program": task["program"], "origin": origin})
            c.edge(db, producer, asset, program, "orchestrates", origin, revision)
    imported(db, producer, origin, revision, len(nodes) + 1,
        db.scalar(select(func.count()).select_from(LineageEdge).where(LineageEdge.producer_id == producer.id, LineageEdge.origin == origin)) or 0, dag_id)
    db.flush()
