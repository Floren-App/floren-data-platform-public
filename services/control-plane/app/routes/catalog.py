"""Search, governance, SLAs, native metadata ingestion, and scoped lineage exploration."""
import json
import re
from datetime import datetime
from typing import Dict, Optional, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import catalog as c, catalog_ingest, catalog_search, rbac
from ..auth import User, Execution, get_execution
from ..db import get_db
from ..deps import get_actor
from ..models import AppGroup, CatalogAsset, CatalogImport, CatalogRunOutput, Workload, Resource, Run, utcnow
from .common import audit

router = APIRouter(prefix="/v1/catalog", tags=["catalog"])


class Governance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=10000)
    domain: str = Field(default="", max_length=128)
    owner_group_id: Optional[str] = None
    steward: str = Field(default="", max_length=256)
    classification: str = Field(default="unclassified", max_length=32, description="A configured classification, or unclassified")
    lineage_role: Literal["data", "operational"] = "data"
    tags: List[str] = Field(default_factory=list, max_length=50)
    status: Literal["draft", "reviewed", "certified", "deprecated"] = "draft"

    @model_validator(mode="after")
    def tags_valid(self):
        if any(not t.strip() or len(t) > 64 for t in self.tags):
            raise ValueError("tags must contain 1 to 64 characters")
        self.tags = sorted(set(t.strip() for t in self.tags))
        return self


class ServiceLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    freshness_minutes: Optional[int] = Field(default=None, ge=1, le=525600)
    success_rate_percent: Optional[float] = Field(default=None, ge=0, le=100)
    window_hours: int = Field(default=168, ge=1, le=8760)
    warning_percent: float = Field(default=80, ge=1, lt=100)
    freshness_basis: Literal["refresh", "checked", "watermark"] = "refresh"
    deadline_time: Optional[str] = Field(default=None, pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(default="UTC", max_length=128)
    weekdays: List[int] = Field(default_factory=lambda: list(range(7)), min_length=1, max_length=7)
    grace_minutes: int = Field(default=15, ge=0, le=1440)
    warning_minutes: int = Field(default=30, ge=0, le=1440)
    quality_required: bool = False
    quality_max_age_minutes: int = Field(default=1440, ge=1, le=525600)
    required_assets: List[str] = Field(default_factory=list, max_length=100)
    enforcement: Literal["monitor", "block_new_runs"] = "monitor"
    block_on_unknown: bool = True

    @model_validator(mode="after")
    def validate_schedule(self):
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("choose an IANA timezone, for example Europe/Amsterdam")
        if any(day < 0 or day > 6 for day in self.weekdays) or len(set(self.weekdays)) != len(self.weekdays):
            raise ValueError("weekdays use unique values 0 (Monday) to 6 (Sunday)")
        if self.enforcement == "block_new_runs" and not self.required_assets:
            raise ValueError("blocking requires explicit upstream catalog assets")
        return self


class DbtImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest: dict
    catalog: Optional[dict] = None
    run_results: Optional[dict] = None
    sources: Optional[dict] = None

    @model_validator(mode="after")
    def bounded(self):
        if len(json.dumps(self.model_dump())) > 8 * 1024 * 1024:
            raise ValueError("dbt metadata import exceeds 8 MB")
        for field in ("nodes", "sources", "exposures", "metrics", "semantic_models"):
            if field in self.manifest and not isinstance(self.manifest[field], dict):
                raise ValueError("dbt manifest collections must be objects")
            for node in self.manifest.get(field, {}).values():
                if not isinstance(node, dict) or not isinstance(node.get("columns", {}), dict) or not isinstance(node.get("depends_on", {}), dict) or not isinstance(node.get("config", {}), dict):
                    raise ValueError("invalid dbt node structure")
                if any(not isinstance(column, dict) for column in node.get("columns", {}).values()):
                    raise ValueError("invalid dbt column structure")
        for artifact in (self.run_results, self.sources):
            if artifact is not None and (not isinstance(artifact.get("results", []), list) or any(not isinstance(v, dict) for v in artifact.get("results", []))):
                raise ValueError("invalid dbt results structure")
        if self.catalog is not None:
            for field in ("nodes", "sources"):
                collection = self.catalog.get(field, {})
                if not isinstance(collection, dict) or any(not isinstance(v, dict) or not isinstance(v.get("columns", {}), dict) or any(not isinstance(col, dict) for col in v.get("columns", {}).values()) for v in collection.values()):
                    raise ValueError("invalid dbt catalog structure")
        return self


class DataReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(default="", max_length=128)
    schema_: str = Field(default="", alias="schema", max_length=256)
    name: str = Field(default="", max_length=512)
    uri: str = Field(default="", max_length=2048)


class AirflowTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=256)
    operator: str = Field(default="", max_length=256)
    upstream_task_ids: List[str] = Field(default_factory=list, max_length=2000)
    program: Optional[str] = Field(default=None, max_length=128)
    workload: Optional[str] = Field(default=None, max_length=128)
    inlets: List[DataReference] = Field(default_factory=list, max_length=1000)
    outlets: List[DataReference] = Field(default_factory=list, max_length=1000)


class AirflowImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dag_id: str = Field(min_length=1, max_length=256)
    schedule: str = Field(default="", max_length=512)
    tasks: List[AirflowTask] = Field(max_length=2000)

    @model_validator(mode="after")
    def unique(self):
        if len({t.task_id for t in self.tasks}) != len(self.tasks):
            raise ValueError("duplicate Airflow task IDs")
        return self


class ProgramReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inputs: List[DataReference] = Field(default_factory=list, max_length=500)
    outputs: List[DataReference] = Field(default_factory=list, max_length=500)


HIDDEN_KINDS = ("file", "folder")


@router.get("/search")
def search(q: str = Query(default="", max_length=300), kind: str = "", domain: str = "", status: str = "",
           classification: str = "", sla_status: str = "", branch: str = "", layer: str = "",
           tag: str = "", owner: str = "", platform: str = "", column: str = "",
           sort: Literal["relevance", "name", "recent"] = "relevance", fuzzy: bool = True,
           offset: int = Query(default=0, ge=0), limit: int = Query(default=30, ge=1, le=100),
           db: Session = Depends(get_db), user: User = Depends(get_actor)):
    c.sync_registry(db)
    c.harvest_artifacts(db)
    groups = {g.id: g.name for g in db.scalars(select(AppGroup))}
    condition = catalog_search.fulltext_condition(db, q, groups)
    candidates, resources = c.visible_assets(db, user, condition=condition)
    ranked = catalog_search.ranked(candidates, q, groups, False)
    # Typos and punctuation variants can miss the native term index. Broaden only
    # within the authorized metadata set, never a global suggestion dictionary.
    if q and not ranked:
        candidates, resources = c.visible_assets(db, user)
        ranked = catalog_search.ranked(candidates, q, groups, fuzzy)
    rank = {asset.id: (score, fields, approximate) for asset, score, fields, approximate in ranked}
    # Files and folders are storage and workload organization: Storage and Workloads show them, the catalog does not.
    matches = [a for a, _, _, _ in ranked if a.kind not in HIDDEN_KINDS]
    # Facets and totals are computed only over objects the caller can read.
    facets = {"layer": sorted({a.catalog_layer["layer_name"] for a in matches if a.catalog_layer.get("layer_name")}), "kind": sorted({a.kind for a in matches}),
              "domain": sorted({a.governance.get("domain") for a in matches if a.governance.get("domain")}),
              "branch": sorted({a.attributes.get("branch") for a in matches if a.attributes.get("branch")}),
              "tag": sorted({t for a in matches for t in a.governance.get("tags", [])}),
              "owner": sorted({groups[a.governance["owner_group_id"]] for a in matches if a.governance.get("owner_group_id") in groups}),
              "platform": sorted({a.attributes.get("source_kind") for a in matches if a.attributes.get("source_kind")})}
    matches = [a for a in matches if (not kind or a.kind == kind)
        and (not domain or a.governance.get("domain") == domain)
        and (not status or a.governance.get("status", "draft") == status)
        and (not classification or a.governance.get("classification", "unclassified") == classification)
        and (not branch or a.attributes.get("branch") == branch)
        and (not layer or layer in (a.catalog_layer.get("layer_id"), a.catalog_layer.get("layer_name")))
        and (not tag or tag in a.governance.get("tags", []))
        and (not owner or owner in (a.governance.get("owner_group_id"), groups.get(a.governance.get("owner_group_id"))))
        and (not platform or platform == a.attributes.get("source_kind"))
        and (not column or any(column.casefold() in col.get("name", "").casefold() for col in a.attributes.get("columns", [])))]
    if sla_status:
        matches = [a for a in matches if c.service_level(db, a, resources[a.resource_id])["status"] == sla_status]
    if sort == "name":
        matches.sort(key=lambda a: (a.name.casefold(), a.id))
    elif sort == "recent":
        matches.sort(key=lambda a: (a.observed_at, a.id), reverse=True)
    result = {"items": [{**c.asset_out(db, a, resources[a.resource_id], user), "score": rank[a.id][0],
                         "matched_fields": rank[a.id][1], "approximate": rank[a.id][2]} for a in matches[offset:offset + limit]],
              "total": len(matches), "offset": offset, "limit": limit, "facets": facets,
              "approximate": any(rank[a.id][2] for a in matches)}
    db.commit()
    return result


@router.get("/suggest")
def suggest(q: str = Query(default="", max_length=300), db: Session = Depends(get_db), user: User = Depends(get_actor)):
    if not q.strip():
        return {"items": []}
    assets, _ = c.visible_assets(db, user)
    groups = {g.id: g.name for g in db.scalars(select(AppGroup))}
    return {"items": catalog_search.suggestions([a for a in assets if a.kind not in HIDDEN_KINDS], q, groups)}


@router.get("/resources/{resource_id}/asset")
def resource_asset(resource_id: str, object_path: Optional[str] = None,
                   db: Session = Depends(get_db), user: User = Depends(get_actor)):
    resource = db.get(Resource, resource_id)
    if not resource or not rbac.allowed(db, user, "read", resource):
        raise HTTPException(404, "catalog object not available")
    if object_path is not None and resource.kind != "schema":
        raise HTTPException(422, "object paths require a schema resource")
    # Resolve the same canonical metadata identity used by data browsing/native imports.
    # Never enumerate or query a data source just to open its lineage.
    ident = c.asset_id(resource.id, "object:" + object_path if object_path is not None else "")
    asset, boundary = c.require_asset(db, ident, user)
    db.commit()
    return c.asset_out(db, asset, boundary, user)


@router.get("/assets/{ident}")
def detail(ident: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    asset, resource = c.require_asset(db, ident, user)
    result = c.asset_out(db, asset, resource, user)
    result["imports"] = [{"origin": i.origin, "scope": i.scope, "revision": i.revision,
        "nodes": i.nodes, "edges": i.edges, "observed_at": i.observed_at} for i in db.scalars(
            select(CatalogImport).where(CatalogImport.resource_id == resource.id))]
    return result


@router.put("/assets/{ident}/governance")
def governance(ident: str, body: Governance, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    asset, resource = c.require_asset(db, ident, user)
    owner = rbac.allowed(db, user, "assign", resource)
    if not owner and not any(rbac.allowed(db, user, action, resource) for action in ("edit", "write", "ddl")):
        raise HTTPException(403, "Contributor or higher required to document this object")
    before, after = asset.governance or {}, body.model_dump()
    from .. import classification as levels
    levels.require_level(db, after["classification"])
    if "columns" in before:
        after["columns"] = before["columns"]  # column classifications are set on their own
    inherited = levels.inherited(db, asset)
    if after["classification"] != levels.UNCLASSIFIED and levels.ranks(db)[after["classification"]] < levels.ranks(db)[inherited]:
        raise HTTPException(422, f"{asset.name} is at least {inherited} because of its columns: an object is never classified lower than its columns")
    protected = {"owner_group_id", "steward", "classification", "status", "lineage_role"}
    defaults = Governance().model_dump()
    if not owner and any(before.get(k, defaults[k]) != after[k] for k in protected):
        raise HTTPException(403, "Owner required to change stewardship, classification or certification")
    if after["owner_group_id"] and not db.get(AppGroup, after["owner_group_id"]):
        raise HTTPException(422, "accountable owner must be an existing app group")
    changed = any(before.get(k, defaults[k]) != after[k] for k in ("description", "domain", "tags"))
    if before.get("status") == "certified" and changed:
        after["status"] = "reviewed"  # Material documentation edits require a fresh Owner certification.
    if after["status"] == "certified" and not (after["description"].strip() and after["domain"].strip()
            and after["owner_group_id"] and after["steward"].strip()
            and levels.highest(db, [after["classification"], inherited]) != levels.UNCLASSIFIED):
        raise HTTPException(422, "certification requires description, domain, owner group, steward and classification")
    asset.governance = after
    c.reindex(asset)
    audit(db, user.username, "catalog.governance", ident, before=before, after=after)
    db.commit()
    return c.asset_out(db, asset, resource, user)


@router.put("/assets/{ident}/sla")
def sla(ident: str, body: ServiceLevel, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    asset, resource = c.require_asset(db, ident, user, "assign")
    if body.success_rate_percent is not None and (resource.kind != "service" or asset.id != c.asset_id(resource.id)):
        raise HTTPException(422, "successful-run SLA applies to deployed programs; datasets support freshness")
    if body.required_assets:
        if resource.kind != 'service' or asset.id != c.asset_id(resource.id):
            raise HTTPException(422, 'Input gates belong on a deployed workload')
        from ..measurements import validate_dependencies
        if ident in body.required_assets:
            raise HTTPException(422, 'A workload cannot gate itself')
        validate_dependencies(db, user, asset, body.required_assets)
    db.scalar(select(CatalogAsset).where(CatalogAsset.id == ident).with_for_update())
    before = asset.sla or {}
    after = body.model_dump()
    changed_schedule = any(before.get(k) != after.get(k) for k in ('deadline_time', 'timezone', 'weekdays', 'grace_minutes', 'freshness_basis'))
    asset.sla = {**after, 'starts_at': utcnow().isoformat() if changed_schedule else before.get('starts_at') or utcnow().isoformat()}
    audit(db, user.username, "catalog.sla", ident, policy=asset.sla)
    from ..sla_monitor import record_evaluation
    record_evaluation(db, asset, resource)
    db.commit()
    return c.asset_out(db, asset, resource, user)


@router.get("/assets/{ident}/lineage")
def lineage(ident: str, direction: Literal["both", "upstream", "downstream"] = "both",
            depth: int = Query(default=3, ge=1, le=32), origin: str = "",
            view: Literal["flow", "full"] = "full",
            since: Optional[datetime] = None, orchestration: bool = False, operational: bool = False, hierarchy: bool = False,
            db: Session = Depends(get_db), user: User = Depends(get_actor)):
    return c.graph(db, user, ident, direction, depth, origin, since, view, orchestration, operational, hierarchy)


def workload(db, resource_id, user):
    resource = db.get(Resource, resource_id)
    pub = db.get(Workload, resource.object_id) if resource and resource.kind == "service" else None
    if not pub or pub.status == "stopped" or not rbac.allowed(db, user, "read", resource):
        raise HTTPException(404, "workload not available")
    rbac.require(db, user, "deploy", resource)
    return pub


def import_result(db, pub, actor, origin):
    audit(db, actor, "catalog.import", pub.resource_id, origin=origin)
    db.commit()
    return {"ok": True, "asset_id": c.asset_id(pub.resource_id)}


@router.post("/resources/{resource_id}/import/dbt")
def import_dbt(resource_id: str, body: DbtImport, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    pub = workload(db, resource_id, user)
    if pub.kind != "dbt":
        raise HTTPException(422, "choose a dbt workload")
    from ..deploying import identity_of
    attester=identity_of(db,pub) if pub.execution_mode=='fixed' else user
    catalog_ingest.dbt(db, pub, body.manifest, body.catalog, body.run_results, body.sources, pub.deployed_sha or "", observation_user=attester)
    return import_result(db, pub, user.username, "dbt_manifest")


@router.post("/resources/{resource_id}/import/airflow")
def import_airflow(resource_id: str, body: AirflowImport, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    pub = workload(db, resource_id, user)
    if pub.kind != "airflow":
        raise HTTPException(422, "choose an Airflow workload")
    catalog_ingest.airflow(db, pub, body.model_dump(by_alias=True), pub.deployed_sha or "")
    return import_result(db, pub, user.username, "airflow_dag")


def execution_workload(db, wl, expected):
    pub = db.get(Workload, wl.workload_id) if wl.workload_id else None
    run = db.get(Run, wl.run_id) if wl.run_id else None
    compatible = pub and (pub.kind == expected or (expected == "dbt" and run and run.kind == "dbt" and run.workload_id == pub.id))
    if not pub or pub.status == "stopped" or not compatible:
        raise HTTPException(403, "native metadata requires that workload's execution token")
    # Run tokens are bound to this workload by get_execution/validate_execution.
    if not wl.run_id and wl.username != pub.identity:
        raise HTTPException(403, "metadata producer does not own this workload")
    return pub


@router.post("/ingest/dbt")
def ingest_dbt(body: DbtImport, db: Session = Depends(get_db), wl: Execution = Depends(get_execution)):
    pub = execution_workload(db, wl, "dbt")
    run = db.get(Run, wl.run_id) if wl.run_id else None
    definition = c.run_definition(pub, run) if run else pub.program_def
    catalog_ingest.dbt(db, pub, body.manifest, body.catalog, body.run_results, body.sources, wl.commit_sha,
                       run.program if run else pub.program, definition, run_id=run.id if run else '',
                       access_snapshot=run.access_snapshot if run else {}, observation_user=None)
    return import_result(db, pub, wl.username, "dbt_manifest")


@router.post("/ingest/airflow")
def ingest_airflow(body: AirflowImport, db: Session = Depends(get_db), wl: Execution = Depends(get_execution)):
    pub = execution_workload(db, wl, "airflow")
    catalog_ingest.airflow(db, pub, body.model_dump(by_alias=True), wl.commit_sha)
    return import_result(db, pub, wl.username, "airflow_dag")


@router.post("/ingest/program")
def ingest_program(body: ProgramReport, db: Session = Depends(get_db), wl: Execution = Depends(get_execution)):
    pub = db.get(Workload, wl.workload_id) if wl.workload_id else None
    run = db.get(Run, wl.run_id) if wl.run_id else None
    if not pub or pub.status == "stopped" or not run or run.workload_id != pub.id:
        raise HTTPException(403, "program lineage requires a running deployed job")
    resource = db.get(Resource, pub.resource_id)
    program = c.upsert(db, resource, kind=pub.kind) if run.program == pub.program else c.upsert(db, resource,
        "program:" + (run.program or run.entrypoint), kind="program", name=run.program or run.entrypoint,
        attributes={"repo": pub.repo.name, "branch": pub.branch, "program": run.program or run.entrypoint})
    if program.id != c.asset_id(resource.id):
        c.edge(db, resource, c.upsert(db, resource, kind=pub.kind), program, "contains", "program_runtime", run.id)
    for field, relation in (("inputs", "reads"), ("outputs", "writes")):
        for ref in getattr(body, field):
            data = catalog_ingest.dataset(db, resource, ref.model_dump(by_alias=True))
            if data:
                c.edge(db, resource, data if field == "inputs" else program, program if field == "inputs" else data,
                       relation, "program_runtime", run.id)
                if field == "outputs" and not db.scalar(select(CatalogRunOutput).where(CatalogRunOutput.run_id == run.id, CatalogRunOutput.asset_id == data.id)):
                    db.add(CatalogRunOutput(run_id=run.id, asset_id=data.id))
    return import_result(db, pub, wl.username, "program_runtime")


class ColumnGovernance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    classification: str = Field(default="unclassified", max_length=32)
    tags: List[str] = Field(default_factory=list, max_length=20, description="For example pii")


class ColumnsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: Dict[str, ColumnGovernance] = Field(description="Per column name; unclassified without tags removes it. "
                                                              "Columns left out keep what they have.")


@router.get("/classifications")
def list_classifications(db: Session = Depends(get_db), _: User = Depends(get_actor)):
    """The configured classifications, from least to most sensitive, with what masked data counts as."""
    from .. import classification as levels
    return {"levels": levels.levels(db)}


class LevelIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=32)
    masked_as: str = Field(default="", max_length=32, description="What masked data of this level counts as; empty: itself")
    description: str = Field(default="", max_length=300)


class LevelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    levels: List[LevelIn] = Field(min_length=1, max_length=20, description="From least to most sensitive")


@router.put("/classifications")
def put_classifications(body: LevelsIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Change the classifications (platform administrators). A level still in use cannot be removed."""
    from .. import classification as levels
    from ..models import PlatformSetting
    if not rbac.resolve_user(db, user).is_admin:
        raise HTTPException(403, "platform administrator required")
    new = levels.validate_levels(db, [l.model_dump() for l in body.levels])
    setting = db.get(PlatformSetting, levels.SETTING) or PlatformSetting(key=levels.SETTING, value={})
    before = levels.levels(db)
    setting.value = {"levels": new}
    db.merge(setting)
    audit(db, user.username, "catalog.classifications", "classifications", before=before, after=new)
    db.commit()
    return {"levels": new}


@router.put("/assets/{ident}/columns")
def put_columns(ident: str, body: ColumnsIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """Classify and tag columns (object Owners). The object's level follows; it is never below its columns."""
    from .. import classification as levels
    asset, resource = c.require_asset(db, ident, user, action="assign")
    governance = dict(asset.governance or {})
    columns = dict(governance.get("columns") or {})
    for name, item in body.columns.items():
        key = name.strip().lower()
        if not key or len(key) > 256:
            raise HTTPException(422, "column names must have 1 to 256 characters")
        levels.require_level(db, item.classification)
        tags = sorted({t.strip().lower() for t in item.tags if t.strip()})
        if any(len(t) > 64 for t in tags):
            raise HTTPException(422, "tags have at most 64 characters")
        if item.classification == levels.UNCLASSIFIED and not tags:
            columns.pop(key, None)
        else:
            columns[key] = {"classification": item.classification, "tags": tags}
    before = (asset.governance or {}).get("columns") or {}
    governance["columns"] = columns
    asset.governance = governance
    c.reindex(asset)
    audit(db, user.username, "catalog.columns", ident, before=before, after=columns)
    db.commit()
    return c.asset_out(db, asset, resource, user)


@router.post("/resources/{resource_id}/asset")
def ensure_asset(resource_id: str, object_path: Optional[str] = Query(default=None, max_length=1024),
                 db: Session = Depends(get_db), user: User = Depends(get_actor)):
    """The catalog entry of an object, created for a table or view not seen yet (object Owners; used by Terraform)."""
    resource = db.get(Resource, resource_id)
    if not resource or not rbac.allowed(db, user, "read", resource):
        raise HTTPException(404, "catalog object not available")
    rbac.require(db, user, "assign", resource)
    c.sync_registry(db)
    if object_path is not None:
        if resource.kind != "schema":
            raise HTTPException(422, "object paths require a schema resource")
        path = object_path.strip()
        if not path:
            raise HTTPException(422, "name the table or view")
        if db.get(CatalogAsset, c.asset_id(resource.id, "object:" + path)) is None:
            c.upsert(db, resource, "object:" + path, kind="table", name=path,
                     attributes={"object_path": path, "schema": resource.name, "origin": "declared"})
            db.flush()
    asset, boundary = c.require_asset(db, c.asset_id(resource.id, "object:" + object_path.strip() if object_path is not None else ""), user)
    db.commit()
    return c.asset_out(db, asset, boundary, user)
