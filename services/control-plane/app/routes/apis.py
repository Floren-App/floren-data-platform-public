"""API management: definitions (/v1/apis), the inbound gateway for internal APIs (/gw/<api>/...), and the
outbound gateway programs use for external APIs (/v1/egress/<api>/...). See app/apis.py."""
from __future__ import annotations

import hmac
import json
import time
from datetime import datetime
from typing import Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .. import apis as core
from .. import rbac
from ..auth import User
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..deps import get_actor, get_orchestrator, get_secret_store
from ..models import ApiCall, ApiDefinition, ApiOperation, Workload, Resource, Run, utcnow
from .common import audit

router = APIRouter(tags=["API management"])


class ApiIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    slug: str = Field(min_length=2, max_length=41)
    direction: Literal["internal", "external"]
    protocol: Literal["rest", "soap"] = "rest"
    description: str = Field(default="", max_length=500)
    layer_id: Optional[str] = None
    environment_id: str = ""
    inbound: core.InboundConfig = Field(default_factory=core.InboundConfig)
    outbound: core.OutboundConfig = Field(default_factory=core.OutboundConfig)


class OperationIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    group: str = Field(default="", max_length=64)
    method: str = "GET"
    path: str = Field(default="/", max_length=256)
    soap_action: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=500)
    internal: core.InternalOperation = Field(default_factory=core.InternalOperation)
    external: core.ExternalOperation = Field(default_factory=core.ExternalOperation)


def _actions(db: Session, user: User, resource: Optional[Resource]) -> List[str]:
    if resource is None:
        return []
    return sorted(a for a in {a for v in rbac.API_ROLES.values() for a in v} if rbac.allowed(db, user, a, resource))


def _endpoint(api: ApiDefinition) -> str:
    if api.direction == "internal":
        return f"{get_settings().api_gateway_url.rstrip('/')}/gw/{api.slug}"
    return f"/v1/egress/{api.slug}"


def _operation_out(db: Session, user: User, api: ApiDefinition, op: ApiOperation) -> dict:
    resource = core.operation_resource(db, api, op)
    return {"id": op.id, "name": op.name, "group": op.group, "method": op.method, "path": op.path,
            "soap_action": op.soap_action, "description": op.description, "config": op.config or {},
            "resource_id": resource.id, "actions": _actions(db, user, resource)}


def _api_out(db: Session, user: User, api: ApiDefinition, operations: bool = True) -> dict:
    resource = core.api_resource(db, api)
    ops = list(db.scalars(select(ApiOperation).where(ApiOperation.api_id == api.id).order_by(ApiOperation.group, ApiOperation.name)))
    visible = [(op, core.operation_resource(db, api, op)) for op in ops]
    visible = [(op, r) for op, r in visible if rbac.allowed(db, user, "read", r)]
    out = {"id": api.id, "slug": api.slug, "name": api.name, "direction": api.direction, "protocol": api.protocol,
           "description": api.description, "config": api.config or {}, "resource_id": resource.id,
           "actions": _actions(db, user, resource), "endpoint": _endpoint(api), "operation_count": len(visible),
           "updated_at": api.updated_at, "created_by": api.created_by}
    if api.direction == "internal" and api.principal_id:
        from ..models import DirectoryPrincipal
        p = db.get(DirectoryPrincipal, api.principal_id)
        out["identity"] = {"principal_id": api.principal_id, "name": p.name if p else "", "username": f"service:{api.principal_id}"}
    if operations:
        out["operations"] = [_operation_out(db, user, api, op) for op, r in visible]
        out["groups"] = [{"name": r.name, "resource_id": r.id, "actions": _actions(db, user, r)}
                         for r in db.scalars(select(Resource).where(Resource.kind == "api-group", Resource.parent_id == resource.id).order_by(Resource.name))
                         if rbac.allowed(db, user, "read", r) or any(child.parent_id == r.id for _, child in visible)]
    return out


def _api(db: Session, api_id: str, user: User, action: str = "read") -> ApiDefinition:
    api = db.get(ApiDefinition, api_id)
    if api is None:
        raise HTTPException(404, "API not found")
    resource = core.api_resource(db, api)
    if not _visible(db, user, api):
        raise HTTPException(404, "API not found")
    if action != "read":
        rbac.require(db, user, action, resource)
    return api


def _visible(db: Session, user: User, api: ApiDefinition) -> bool:
    resource = core.api_resource(db, api)
    if rbac.allowed(db, user, "read", resource):
        return True
    # Access to one endpoint group or operation shows its API (with only that part).
    children = list(db.scalars(select(Resource).where(Resource.parent_id == resource.id)))
    grand = [r for c in children if c.kind == "api-group" for r in db.scalars(select(Resource).where(Resource.parent_id == c.id))]
    return any(rbac.allowed(db, user, "read", r) for r in children + grand)


@router.get("/v1/apis")
def list_apis(db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    rbac.api_root(db)
    out = [_api_out(db, user, a, operations=False) for a in db.scalars(select(ApiDefinition).order_by(ApiDefinition.name))
           if _visible(db, user, a)]
    root_actions = _actions(db, user, db.get(Resource, "apis"))
    db.commit()
    return {"apis": out, "can_create": "create" in root_actions}


def _configure(db: Session, user: User, api: ApiDefinition, body: ApiIn) -> None:
    if not body.name.strip():
        core.invalid({"name": "enter a name"})
    previous_config = api.config or {}
    api.name, api.description, api.protocol = body.name.strip(), body.description, body.protocol
    if api.direction == "internal":
        api.config = {"inbound": core.validate_inbound(db, user, body.inbound)}
    else:
        api.config = {"outbound": core.validate_outbound(db, user, body.outbound)}
    from ..layers import canonical, require_layer
    chosen = require_layer(db, body.layer_id or body.environment_id or previous_config.get("layer_id")).id
    previous = previous_config.get("layer_id") or previous_config.get("environment_id")
    if api.id and previous and chosen != canonical(db, previous):
        raise HTTPException(409, "an API cannot change layers")
    api.config = {**api.config, "layer_id": chosen}
    if (api.config.get("inbound") or {}).get("anonymous"):
        from ..layers import is_production
        if is_production(db, chosen):
            core.invalid({"inbound.auth": "anonymous callers are not allowed in a production layer"})
        if not (previous_config.get("inbound") or {}).get("anonymous"):
            audit(db, user.username, "api.anonymous_enabled", api.id or api.slug, layer=chosen)
    if body.environment_id:
        api.config["environment_id"] = body.environment_id
    api.updated_at = utcnow()


@router.post("/v1/apis", status_code=201)
def create_api(body: ApiIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    from ..ownership import require_create
    require_create(db,user,rbac.api_root(db))
    if not core.SLUG.fullmatch(body.slug):
        core.invalid({"slug": "lowercase letters, digits and dashes, starting with a letter"})
    if db.scalar(select(ApiDefinition).where(ApiDefinition.slug == body.slug)):
        core.invalid({"slug": "this address is already used by another API"})
    api = ApiDefinition(slug=body.slug, direction=body.direction, created_by=user.username, name=body.name)
    _configure(db, user, api, body)
    db.add(api)
    db.flush()
    resource = core.api_resource(db, api)
    rbac.own(db, user, resource)
    if api.direction == "internal":
        core.identity_principal(db, api)
    audit(db, user.username, "api.create", api.id, slug=api.slug, direction=api.direction, protocol=api.protocol)
    db.commit()
    return _api_out(db, user, api)


@router.get("/v1/apis/{api_id}")
def get_api(api_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = db.get(ApiDefinition, api_id)
    if api is None or not _visible(db, user, api):
        raise HTTPException(404, "API not found")
    out = _api_out(db, user, api)
    db.commit()
    return out


@router.put("/v1/apis/{api_id}")
def update_api(api_id: str, body: ApiIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = _api(db, api_id, user, "edit")
    if body.slug != api.slug or body.direction != api.direction:
        raise HTTPException(409, "an API's address and direction are fixed; create a new API instead")
    _configure(db, user, api, body)
    core.api_resource(db, api)
    if api.direction == "internal":
        core.identity_principal(db, api)
    audit(db, user.username, "api.update", api.id)
    db.commit()
    return _api_out(db, user, api)


@router.delete("/v1/apis/{api_id}", status_code=204)
def delete_api(api_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = _api(db, api_id, user, "delete")
    for op in db.scalars(select(ApiOperation).where(ApiOperation.api_id == api.id)):
        core.delete_resource(db, op.resource_id)
        db.delete(op)
    db.flush()
    core.prune_groups(db, api)
    core.delete_resource(db, api.resource_id)
    if api.principal_id:
        from ..models import DirectoryPrincipal
        p = db.get(DirectoryPrincipal, api.principal_id)
        if p is not None:
            p.active = False   # the identity can no longer act; bindings stay visible for review
    audit(db, user.username, "api.delete", api.id, slug=api.slug)
    db.delete(api)
    db.commit()


def _layer(db: Session, resource: Resource) -> str:
    from ..layers import resource_layer
    return resource_layer(db, resource)


def _validate_operation(db: Session, user: User, api: ApiDefinition, body: OperationIn, current: Optional[ApiOperation]) -> dict:
    errors: Dict[str, str] = {}
    if not core.OPERATION_NAME.fullmatch(body.name):
        errors["name"] = "letters, digits, '.', '_' and '-', starting with a letter"
    elif db.scalar(select(ApiOperation).where(ApiOperation.api_id == api.id, ApiOperation.name == body.name,
                                              ApiOperation.id != (current.id if current else ""))):
        errors["name"] = "this API already has an operation with this name"
    if body.group and not core.GROUP_NAME.fullmatch(body.group):
        errors["group"] = "letters, digits, spaces, '.', '_' and '-'"
    if api.protocol == "soap":
        if body.method != "POST":
            errors["method"] = "SOAP operations use POST"
        if api.direction == "internal" and not body.soap_action and not body.name:
            errors["soap_action"] = "required"
    elif body.method not in core.METHODS:
        errors["method"] = "choose GET, POST, PUT, PATCH or DELETE"
    core.validate_path(body.path, errors)
    signature = lambda path: core.re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")
    for existing in db.scalars(select(ApiOperation).where(ApiOperation.api_id == api.id,
            ApiOperation.id != (current.id if current else ""))):
        if api.protocol == "rest" and existing.method == body.method and signature(existing.path) == signature(body.path):
            errors["path"] = "this method and path are already declared by another operation"
        elif api.protocol == "soap" and body.soap_action and existing.soap_action == body.soap_action:
            errors["soap_action"] = "this SOAP action is already declared by another operation"
    if api.direction == "internal":
        pub = db.get(Workload, body.internal.workload_id) if body.internal.workload_id else None
        if pub is None:
            errors["internal.workload_id"] = "choose the deployed program this endpoint runs"
        else:
            if pub.kind not in ("script", "notebook", "dbt"):
                errors["internal.workload_id"] = "only on-demand programs (scripts, notebooks, dbt) can back an endpoint"
            elif not rbac.allowed(db, user, "execute", rbac.workload_resource(db, pub)):
                errors["internal.workload_id"] = "you may only expose programs you may run yourself"
            elif _layer(db, rbac.workload_resource(db, pub)) != _layer(db, core.api_resource(db, api)):
                # The API's managed identity is bound to its layer; it could never run this program.
                errors["internal.workload_id"] = "choose a program deployed in this API's layer"
            names = {a.get("name") for a in (pub.program_def or {}).get("args", [])}
            for arg, rule in body.internal.args.items():
                if arg not in names:
                    errors[f"internal.args.{arg}"] = "the program has no such argument"
                elif rule and not (rule.startswith("=") or rule.split(".", 1)[0] in ("path", "query", "body", "header")):
                    errors[f"internal.args.{arg}"] = "use path.x, query.x, body.x, header.x or =value"
        config = body.internal.model_dump()
    else:
        for i, v in enumerate(body.external.headers):
            if not core.HEADER_NAME.fullmatch(v.name):
                errors[f"external.headers.{i}.name"] = "invalid header name"
            core.check_secret(db, user, v.secret, f"external.headers.{i}.secret", errors)
        config = body.external.model_dump()
    if errors:
        core.invalid(errors, "operation is incomplete")
    return config


@router.post("/v1/apis/{api_id}/operations", status_code=201)
def create_operation(api_id: str, body: OperationIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = _api(db, api_id, user)
    target = core.find_group(db, api, body.group.strip()) or core.api_resource(db, api)
    from ..ownership import require_create
    require_create(db,user,target)
    op = ApiOperation(api_id=api.id, name=body.name, group=body.group.strip(), method=body.method,
                      path=body.path, soap_action=body.soap_action, description=body.description)
    op.config = _validate_operation(db, user, api, body, None)
    db.add(op)
    db.flush()
    resource = core.operation_resource(db, api, op)
    rbac.own(db,user,resource)
    if body.group.strip():
        parent = core.find_group(db,api,body.group.strip())
        if parent and not (parent.config or {}).get("owner_group_id"):
            rbac.own(db,user,parent)
    audit(db, user.username, "api.operation.create", op.id, api=api.slug, operation=op.name)
    db.commit()
    return _operation_out(db, user, api, op)


@router.put("/v1/apis/{api_id}/operations/{op_id}")
def update_operation(api_id: str, op_id: str, body: OperationIn, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = _api(db, api_id, user)
    op = db.get(ApiOperation, op_id)
    if op is None or op.api_id != api.id:
        raise HTTPException(404, "operation not found")
    rbac.require(db, user, "edit", core.operation_resource(db, api, op))
    if body.group.strip() != op.group:
        # Moving into another group changes inherited access: require create rights there, like folders.
        target = core.find_group(db, api, body.group.strip()) or core.api_resource(db, api)
        rbac.require(db, user, "create", target)
    op.config = _validate_operation(db, user, api, body, op)
    op.name, op.group, op.method, op.path = body.name, body.group.strip(), body.method, body.path
    op.soap_action, op.description = body.soap_action, body.description
    core.operation_resource(db, api, op)
    db.flush()
    core.prune_groups(db, api)
    audit(db, user.username, "api.operation.update", op.id, api=api.slug, operation=op.name)
    db.commit()
    return _operation_out(db, user, api, op)


@router.delete("/v1/apis/{api_id}/operations/{op_id}", status_code=204)
def delete_operation(api_id: str, op_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = _api(db, api_id, user)
    op = db.get(ApiOperation, op_id)
    if op is None or op.api_id != api.id:
        raise HTTPException(404, "operation not found")
    rbac.require(db, user, "delete", core.operation_resource(db, api, op))
    core.delete_resource(db, op.resource_id)
    db.delete(op)
    db.flush()
    core.prune_groups(db, api)
    audit(db, user.username, "api.operation.delete", op_id, api=api.slug)
    db.commit()


@router.get("/v1/apis/{api_id}/calls")
def list_calls(api_id: str, db: Session = Depends(get_db), user: User = Depends(get_actor)):
    user = rbac.resolve_user(db, user)
    api = _api(db, api_id, user)
    calls = db.scalars(select(ApiCall).where(ApiCall.api_id == api.id).order_by(ApiCall.created_at.desc()).limit(100))
    ops = list(db.scalars(select(ApiOperation).where(ApiOperation.api_id == api.id)))
    names = {o.id: o.name for o in ops if rbac.allowed(db, user, "read", core.operation_resource(db, api, o))}
    full_access = rbac.allowed(db, user, "read", core.api_resource(db, api))
    out = []
    for c in calls:
        if c.operation_id not in names and not full_access:
            continue
        run = db.get(Run, c.run_id) if c.run_id else None
        out.append({"id": c.id, "operation": names.get(c.operation_id, "?"), "caller": c.caller, "status_code": c.status_code,
                    "run_id": c.run_id, "run_status": run.status if run else None, "at": c.created_at})
    return out


# --- inbound gateway: internal APIs -------------------------------------------------------------------

def _response_json(orch, run: Run) -> Optional[object]:
    from ..workspace_files import read_output
    data = read_output(orch, None, run, "response.json", core.MAX_BODY)
    if not data:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return data.decode("utf-8", "replace")


def _response_status(orch, run: Run) -> int:
    """The HTTP status code the program chose for its answer (fdp.respond(..., status=...)), else 200."""
    from ..workspace_files import read_output
    raw = (read_output(orch, None, run, "response.status", 16) or b"").strip()
    if not raw:
        return 200
    try:
        code = int(raw)
    except ValueError:
        return 502
    return code if 200 <= code <= 599 and code not in (204, 304) else 502  # those carry no body


def _reply(api: ApiDefinition, op_name: str, status: int, payload: dict, headers: Optional[dict] = None) -> Response:
    if api.protocol == "soap":
        version = (api.config.get("inbound") or {}).get("soap_version", "1.1")
        if status >= 400:
            return Response(core.soap_fault(version, status < 500, str(payload.get("error", "error"))), status_code=500 if version == "1.1" else (400 if status < 500 else 500),
                            media_type=core.soap_media_type(version))
        body = core.to_xml({"runId": payload.get("run_id"), "status": payload.get("status"), "statusUrl": payload.get("status_url"),
                            "result": payload.get("result")}, f"{op_name}Response")
        return Response(core.soap_envelope(version, body), status_code=status, media_type=core.soap_media_type(version), headers=headers)
    return JSONResponse(payload, status_code=status, headers=headers)


def _run_payload(api: ApiDefinition, run: Run, orch) -> dict:
    """status: the run's; status_code: the HTTP status of the program's answer once it finished."""
    payload = {"run_id": run.id, "status": run.status, "status_url": f"{_endpoint(api)}/_runs/{run.id}"}
    if run.status == "succeeded":
        payload["status_code"] = _response_status(orch, run)
        payload["result"] = _response_json(orch, run)
        if payload["status_code"] >= 400:
            payload["error"] = payload["result"] if isinstance(payload["result"], str) else f"the program answered {payload['status_code']}"
    elif run.status in ("failed", "stopped"):
        payload["status_code"] = 502
    return payload


def handle_inbound(slug: str, path: str, method: str, headers: Dict[str, str], query: Dict[str, str], body: bytes) -> Response:
    from ..routes.deploy import _start_deployed
    from ..reaper import refresh_run
    from ..deps import get_orchestrator as orchestrator, get_secret_store as secret_store
    orch, store = orchestrator(), secret_store()
    with SessionLocal() as db:
        api = db.scalar(select(ApiDefinition).where(ApiDefinition.slug == slug, ApiDefinition.direction == "internal"))
        if api is None:
            return JSONResponse({"error": "API not found"}, status_code=404)
        via_gateway = hmac.compare_digest(headers.get("x-fdp-gateway-proof", ""), core.gateway_proof())
        op_name = "Operation"
        try:
            caller = core.authenticate(api, headers, store, via_gateway)
            if path.startswith("_runs/") and method == "GET":
                call = db.scalar(select(ApiCall).where(ApiCall.api_id == api.id, ApiCall.run_id == path[6:], ApiCall.caller == caller))
                run = db.get(Run, call.run_id) if call else None
                if run is None:
                    raise HTTPException(404, "run not found for this caller")
                refresh_run(db, orch, run)
                db.commit()
                return _reply(api, op_name, 200, _run_payload(api, run, orch))
            ops = list(db.scalars(select(ApiOperation).where(ApiOperation.api_id == api.id)))
            sources: Dict[str, dict] = {"path": {}, "query": dict(query), "header": {k.lower(): v for k, v in headers.items()}, "body": {}}
            if api.protocol == "soap":
                if method != "POST":
                    raise HTTPException(405, "SOAP endpoints accept POST")
                element, values = core.soap_request(body)
                op = core.match_soap(ops, headers.get("soapaction", "") or _action_param(headers.get("content-type", "")), element)
                sources["body"] = values if isinstance(values, dict) else {}
            else:
                op, sources["path"] = core.match_rest(ops, method, path)
                if body and op is not None:
                    try:
                        parsed = json.loads(body)
                    except ValueError:
                        raise HTTPException(400, "the request body must be JSON") from None
                    sources["body"] = parsed if isinstance(parsed, dict) else {}
            if op is None:
                raise HTTPException(404, "no operation matches this request")
            op_name = op.name
            cfg = core.InternalOperation.model_validate(op.config or {})
            pub = db.get(Workload, cfg.workload_id)
            if pub is None:
                raise HTTPException(503, "this endpoint's program is no longer deployed")
            identity = core.identity_user(db, api)
            names = [a.get("name") for a in (pub.program_def or {}).get("args", [])]
            args = core.collect_args(cfg.args, names, sources)
            try:
                run = _start_deployed(db, orch, pub, identity, args, cfg.pool or None)
            except HTTPException as exc:
                if exc.status_code == 403:
                    raise HTTPException(403, f"the API's managed identity may not run {pub.title}; grant it Executor through an app group") from None
                raise
            db.add(ApiCall(api_id=api.id, operation_id=op.id, run_id=run.id, caller=caller, status_code=202))
            audit(db, identity.username, "api.call", api.id, operation=op.name, caller=caller, run=run.id)
            db.commit()
            if cfg.mode == "sync":
                deadline = time.monotonic() + cfg.timeout_seconds
                while run.status in ("pending", "running") and time.monotonic() < deadline:
                    time.sleep(0.5)
                    db.refresh(run)
                    refresh_run(db, orch, run)
                    db.commit()
                if run.status == "succeeded":
                    answer = _run_payload(api, run, orch)
                    return _reply(api, op_name, answer["status_code"], answer)  # the program may answer with an error
                if run.status in ("failed", "stopped"):
                    return _reply(api, op_name, 502, {**_run_payload(api, run, orch), "error": f"the program {run.status}"})
            payload = _run_payload(api, run, orch)
            return _reply(api, op_name, 202, payload, {"Location": payload["status_url"]})
        except HTTPException as exc:
            db.rollback()
            if exc.status_code in (401, 403):
                audit(db, "anonymous", "api.denied", api.id, reason=str(exc.detail)[:200])
                db.commit()
            detail = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail)
            response = _reply(api, op_name, exc.status_code, {"error": detail})
            for k, v in (exc.headers or {}).items():
                response.headers[k] = v
            return response


def _action_param(content_type: str) -> str:
    import re
    m = re.search(r'action="?([^";]+)"?', content_type)
    return m.group(1) if m else ""


@router.api_route("/gw/{slug}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
@router.api_route("/gw/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def inbound(slug: str, request: Request, path: str = ""):
    body = await request.body()
    if len(body) > core.MAX_BODY:
        return JSONResponse({"error": "request body exceeds 10 MB"}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    return await run_in_threadpool(handle_inbound, slug, path, request.method, headers, dict(request.query_params), body)


# --- outbound gateway: external APIs ------------------------------------------------------------

@router.api_route("/v1/egress/{slug}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def egress(slug: str, path: str, request: Request, user: User = Depends(get_actor)):
    """Programs (and people testing) call an external API as themselves; needs Consumer on the operation."""
    body = await request.body()
    if len(body) > core.MAX_BODY:
        raise HTTPException(413, "request body exceeds 10 MB")
    return await run_in_threadpool(_egress, slug, path, request.method, dict(request.headers), list(request.query_params.multi_items()), body, user)


def _egress(slug: str, path: str, method: str, headers: Dict[str, str], query, body: bytes, user: User) -> Response:
    started = time.monotonic()
    with SessionLocal() as db:
        user = rbac.resolve_user(db, user)
        api = db.scalar(select(ApiDefinition).where(ApiDefinition.slug == slug, ApiDefinition.direction == "external"))
        if api is None or not _visible(db, user, api):
            raise HTTPException(404, "API not found")
        ops = list(db.scalars(select(ApiOperation).where(ApiOperation.api_id == api.id)))
        if api.protocol == "soap":
            if method != "POST":
                raise HTTPException(405, "SOAP endpoints accept POST", headers={"Allow": "POST"})
            op = next((o for o in ops if o.name == path.strip("/")), None)
            upstream = op.path if op else ""
        else:
            op, _ = core.match_rest(ops, method, path)
            upstream = path
        if op is None:
            raise HTTPException(404, f"{api.name} has no operation for {method} /{path}")
        resource = core.operation_resource(db, api, op)
        if not rbac.allowed(db, user, "call", resource):
            audit(db, user.username, "api.egress.denied", api.id, operation=op.name)
            db.commit()
            raise HTTPException(403, f"{user.username} may not call {api.name} {op.name}; ask for the Consumer role")
        db.commit()
        status, reply, content = core.call_external(api, op, upstream, query, headers, body, get_secret_store())
        audit(db, user.username, "api.egress", api.id, operation=op.name, status=status, ms=int((time.monotonic() - started) * 1000))
        if 200 <= status < 300:
            from ..catalog import successful_api_call
            successful_api_call(db, api, op, user=user)
        db.commit()
    return Response(content, status_code=status, headers=reply)
