"""Send native lineage metadata. These calls never grant data permissions.

    python -m fdp.lineage dbt --target-dir target
    fdp.lineage.report(inputs=[{"source": "lake", "schema": "finance", "name": "sales"}])
"""
import argparse
import json
from pathlib import Path

from . import _client


def send(kind, payload):
    with _client() as client:
        response = client.post("/v1/catalog/ingest/" + kind, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()


def report(inputs=None, outputs=None):
    """Report the registered source/schema/object inputs and outputs of this deployed job."""
    return send("program", {"inputs": inputs or [], "outputs": outputs or []})


def dbt(target_dir="target", results=None):
    root = Path(target_dir)
    payload = {}
    for name, filename in (("manifest", "manifest.json"), ("catalog", "catalog.json"),
                           ("run_results", "run_results.json"), ("sources", "sources.json")):
        path = Path(results) if name == "run_results" and results else root / filename
        if path.is_file():
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("dbt artifact exceeds 8 MB")
            payload[name] = json.loads(path.read_text())
    if "manifest" not in payload:
        raise ValueError("manifest.json is required for dbt lineage")
    # Never upload SQL, credentials, environment variables, or unrelated project configuration.
    manifest = payload["manifest"]
    clean = {}
    fields = {"name", "resource_type", "unique_id", "database", "schema", "alias", "identifier",
              "description", "columns", "depends_on"}
    for collection in ("nodes", "sources", "exposures", "metrics", "semantic_models"):
        clean[collection] = {uid: {**{k: v for k, v in node.items() if k in fields and k != "columns"},
            "columns": {name: {k: v for k, v in col.items() if k in {"name", "data_type", "description"}} for name, col in node.get("columns", {}).items()},
            "config": {k: v for k,v in node.get("config", {}).items() if k in {"materialized", "severity", "warn_if", "error_if"}}} for uid, node in manifest.get(collection, {}).items()}
    payload["manifest"] = clean
    if "run_results" in payload:
        payload["run_results"] = {"metadata": {k:v for k,v in payload['run_results'].get('metadata', {}).items() if k in {'invocation_id', 'generated_at'}},
            "results": [{k: v for k, v in result.items() if k in {"unique_id", "status", "failures", "execution_time", "timing"}} for result in payload["run_results"].get("results", [])]}
    if "sources" in payload:
        payload["sources"] = {"metadata": {k:v for k,v in payload['sources'].get('metadata', {}).items() if k in {'invocation_id', 'generated_at'}},
            "results": [{k: v for k, v in result.items() if k in {"unique_id", "status", "max_loaded_at"}} for result in payload["sources"].get("results", [])]}
    if "catalog" in payload:
        payload["catalog"] = {collection: {uid: {"columns": {name: {k: v for k, v in col.items() if k in {"name", "type"}} for name, col in node.get("columns", {}).items()}} for uid, node in payload["catalog"].get(collection, {}).items()} for collection in ("nodes", "sources")}
    return send("dbt", payload)


def reference(value):
    """Airflow native Dataset/Asset URI or lineage Table entity -> metadata identifier."""
    if isinstance(value, dict) and "source" in value:
        return {k: str(value[k]) for k in ("source", "schema", "name") if k in value}
    uri = value if isinstance(value, str) else getattr(value, "uri", None) or getattr(value, "url", None)
    if uri:
        from urllib.parse import urlsplit
        parsed = urlsplit(str(uri))
        if parsed.scheme == "fdp" and not (parsed.username or parsed.password or parsed.query or parsed.fragment):
            return {"uri": str(uri)}
    if getattr(value, "database", None) and getattr(value, "cluster", None) and getattr(value, "name", None):
        return {"source": str(value.cluster), "schema": str(value.database), "name": str(value.name)}
    return None


def airflow_payload(dag, operator=None, inlets=None, outlets=None):
    tasks = []
    for task in dag.tasks:
        def references(values):
            if not isinstance(values, (list, tuple, set)):
                values = [values] if values else []
            return [ref for ref in (reference(v) for v in values) if ref]
        tasks.append({"task_id": task.task_id, "operator": getattr(task, "task_type", type(task).__name__),
            "upstream_task_ids": sorted(task.upstream_task_ids),
            "program": getattr(task, "program", None), "workload": getattr(task, "workload", None),
            "inlets": references(inlets if task is operator and inlets is not None else getattr(task, "inlets", [])),
            "outlets": references(outlets if task is operator and outlets is not None else getattr(task, "outlets", []))})
    return {"dag_id": dag.dag_id, "schedule": str(getattr(dag, "schedule_interval", "")), "tasks": tasks}


def airflow_dag(dag, operator=None, inlets=None, outlets=None):
    return send("airflow", airflow_payload(dag, operator, inlets, outlets))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["dbt"])
    parser.add_argument("--target-dir", default="target")
    parser.add_argument("--results")
    args = parser.parse_args()
    dbt(args.target_dir, args.results)
